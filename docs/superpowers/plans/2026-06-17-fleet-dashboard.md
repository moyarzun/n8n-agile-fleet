# Fleet Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar el endpoint `/run` bloqueante con un sistema async + SSE que permita monitorear múltiples jobs en tiempo real desde una UI web servida por el mismo contenedor Docker.

**Architecture:** `fleet_api.py` mantiene `_jobs: Dict[str, JobState]` en memoria y un pipeline de broadcast SSE basado en `asyncio.Queue`. `engine.stream()` corre en `ThreadPoolExecutor` y publica eventos al event loop principal via `loop.call_soon_threadsafe`. Una tarea async `_broadcast_loop()` consume esos eventos y los fan-out a todos los clientes `EventSource` conectados.

**Tech Stack:** FastAPI, asyncio, ThreadPoolExecutor, Server-Sent Events, HTML/CSS/JS vanilla. Sin dependencias nuevas.

## Global Constraints

- Sin dependencias nuevas en `requirements.txt` — solo las ya existentes.
- `GET /health` no cambia.
- `POST /run` con header `X-Wait: true` mantiene la respuesta `FleetResponse` bloqueante para compatibilidad con `mcp_fleet_server.py`.
- Sin modificar `langgraph_fleet.py`.
- El HTML del dashboard va inline en `fleet_api.py` como string — sin archivos estáticos separados.
- Jobs se eliminan de memoria 1 hora después de finalizar.

---

## File Map

| Archivo | Acción | Responsabilidad |
|---|---|---|
| `agile_scripts/fleet_api.py` | Reescritura | JobState, store, broadcast, worker, endpoints, HTML |
| `agile_scripts/mcp_fleet_server.py` | Modificar | Agregar header `X-Wait: true` al POST /run |
| `tests/test_fleet_api.py` | Crear | Tests de endpoints, SSE headers, backward compat |

---

## Task 1: JobState + store + broadcast pipeline

**Files:**
- Modify: `agile_scripts/fleet_api.py` (sección inicial — imports, modelos, estado global, broadcast)
- Create: `tests/test_fleet_api.py`

**Interfaces:**
- Produces:
  - `JobState(job_id, ticket_id)` — dataclass con campos mutables
  - `_jobs: Dict[str, JobState]` — store global
  - `_subscribers: List[asyncio.Queue]` — clientes SSE conectados
  - `_event_queue: asyncio.Queue` — pipeline thread→async
  - `_broadcast_loop()` — tarea async que hace fan-out de eventos

---

- [ ] **Step 1: Escribir test de JobState**

Crear `tests/__init__.py` vacío y `tests/test_fleet_api.py`:

```python
import pytest
from datetime import datetime, timezone


def test_job_state_initial_values():
    """JobState arranca con los campos correctos."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import JobState

    job = JobState(job_id="abc-123", ticket_id="SCRUM-1")

    assert job.job_id == "abc-123"
    assert job.ticket_id == "SCRUM-1"
    assert job.status == "queued"
    assert job.phase == ""
    assert job.iteration == 0
    assert job.files_count == 0
    assert job.logs == []
    assert job.finished_at is None
    assert job.summary == ""
    # started_at debe ser un ISO timestamp válido
    datetime.fromisoformat(job.started_at)


def test_job_state_to_dict():
    """to_dict() devuelve todos los campos esperados."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import JobState

    job = JobState(job_id="abc-123", ticket_id="SCRUM-1")
    d = job.to_dict()

    assert set(d.keys()) == {
        "job_id", "ticket_id", "status", "phase",
        "iteration", "files_count", "logs",
        "started_at", "finished_at", "summary",
    }
    assert d["logs"] == []
```

- [ ] **Step 2: Ejecutar test — debe fallar**

```bash
cd /Users/moyarzun/Documents/Claude/Projects/n8n
python -m pytest tests/test_fleet_api.py::test_job_state_initial_values -v
```

Esperado: `ImportError` o `AttributeError` — `fleet_api.py` no tiene `JobState` aún.

- [ ] **Step 3: Reescribir fleet_api.py — sección imports + modelos + estado global**

Reemplazar el contenido completo de `agile_scripts/fleet_api.py` con:

```python
import os
import uuid
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from langgraph_fleet import build_architecture, FleetState

app = FastAPI(title="LangGraph Fleet API")
_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------

class TicketRequest(BaseModel):
    ticket_id: str
    workspace: str = "/workspace"


class FleetResponse(BaseModel):
    ticket_id: str
    approved: bool
    iterations: int
    summary: str


@dataclass
class JobState:
    job_id: str
    ticket_id: str
    status: str = "queued"
    phase: str = ""
    iteration: int = 0
    files_count: int = 0
    logs: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "ticket_id": self.ticket_id,
            "status": self.status,
            "phase": self.phase,
            "iteration": self.iteration,
            "files_count": self.files_count,
            "logs": self.logs[-100:],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

_jobs: Dict[str, JobState] = {}
_subscribers: List[asyncio.Queue] = []
_event_queue: asyncio.Queue = asyncio.Queue()
_main_loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Broadcast pipeline (thread-safe: thread → asyncio)
# ---------------------------------------------------------------------------

async def _broadcast_loop() -> None:
    """Tarea async permanente: consume _event_queue y fan-out a suscriptores SSE."""
    while True:
        event_name, data = await _event_queue.get()
        msg = f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
        dead: List[asyncio.Queue] = []
        for q in list(_subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass


def _emit(event_name: str, data: dict) -> None:
    """Llamada desde threads: encola evento en el loop principal."""
    if _main_loop and not _main_loop.is_closed():
        _main_loop.call_soon_threadsafe(_event_queue.put_nowait, (event_name, data))


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    asyncio.create_task(_broadcast_loop())
    asyncio.create_task(_cleanup_loop())


async def _cleanup_loop() -> None:
    """Elimina jobs finalizados hace más de 1 hora."""
    while True:
        await asyncio.sleep(300)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        stale = [
            jid for jid, job in list(_jobs.items())
            if job.finished_at and datetime.fromisoformat(job.finished_at) < cutoff
        ]
        for jid in stale:
            _jobs.pop(jid, None)
```

- [ ] **Step 4: Ejecutar tests — deben pasar**

```bash
python -m pytest tests/test_fleet_api.py -v
```

Esperado: `2 passed`.

- [ ] **Step 5: Commit**

```bash
cd /Users/moyarzun/Documents/Claude/Projects/n8n
git add agile_scripts/fleet_api.py tests/
git commit -m "feat: add JobState dataclass and SSE broadcast pipeline"
```

---

## Task 2: Worker — ejecutar engine.stream() en thread pool

**Files:**
- Modify: `agile_scripts/fleet_api.py` (agregar función `_run_fleet_worker`)
- Modify: `tests/test_fleet_api.py`

**Interfaces:**
- Consumes: `JobState`, `_jobs`, `_emit`, `build_architecture`, `FleetState`
- Produces: `_run_fleet_worker(job_id, ticket_id, workspace)` — función síncrona para ThreadPoolExecutor; retorna `dict` con `{approved, iterations, summary}` para el modo X-Wait

---

- [ ] **Step 1: Escribir test del worker con mock**

Agregar al final de `tests/test_fleet_api.py`:

```python
def test_run_fleet_worker_updates_job_state(monkeypatch):
    """Worker actualiza status y emite eventos correctamente."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api

    job = fleet_api.JobState(job_id="test-1", ticket_id="SCRUM-99")
    fleet_api._jobs["test-1"] = job

    emitted = []
    monkeypatch.setattr(fleet_api, "_emit", lambda name, data: emitted.append((name, data)))

    # Mock engine que emite un evento developer y uno reviewer con aprobación
    class FakeMsg:
        content = "Test message"

    def fake_stream(initial, stream_mode):
        yield {"dynamic_developer": {"messages": [FakeMsg()], "loop_iterations": 1, "applied_files": ["a.py", "b.py"]}}
        yield {"quality_reviewer": {"messages": [FakeMsg()], "is_approved": True, "reviewer_feedback": "OK"}}

    class FakeEngine:
        def stream(self, initial, stream_mode):
            return fake_stream(initial, stream_mode)

    monkeypatch.setattr(fleet_api, "build_architecture", lambda: FakeEngine())

    result = fleet_api._run_fleet_worker("test-1", "SCRUM-99", "/workspace")

    assert job.status == "approved"
    assert job.iteration == 1
    assert job.files_count == 2
    assert job.finished_at is not None
    assert result["approved"] is True
    assert result["iterations"] == 1

    event_names = [e[0] for e in emitted]
    assert "job_started" in event_names
    assert "job_update" in event_names
    assert "job_finished" in event_names
```

- [ ] **Step 2: Ejecutar test — debe fallar**

```bash
python -m pytest tests/test_fleet_api.py::test_run_fleet_worker_updates_job_state -v
```

Esperado: `AttributeError: module 'fleet_api' has no attribute '_run_fleet_worker'`

- [ ] **Step 3: Agregar _run_fleet_worker a fleet_api.py**

Agregar al final de `agile_scripts/fleet_api.py` (después de `_cleanup_loop`):

```python
# ---------------------------------------------------------------------------
# Worker — corre en ThreadPoolExecutor
# ---------------------------------------------------------------------------

def _run_fleet_worker(job_id: str, ticket_id: str, workspace: str) -> dict:
    """Ejecuta engine.stream() sincrónicamente. Actualiza _jobs y emite eventos SSE."""
    job = _jobs[job_id]
    job.status = "running"

    _emit("job_started", {
        "job_id": job_id,
        "ticket_id": ticket_id,
        "started_at": job.started_at,
    })

    try:
        engine = build_architecture()
        initial: FleetState = {
            "messages": [],
            "ticket_id": ticket_id,
            "workspace_path": workspace,
            "acceptance_criteria": "",
            "required_agents": [],
            "current_code_diff": {},
            "applied_files": [],
            "reviewer_feedback": "",
            "is_approved": False,
            "loop_iterations": 0,
        }
        final_state: dict = dict(initial)

        for step_event in engine.stream(initial, stream_mode="updates"):
            for node_name, data in step_event.items():
                if data.get("loop_iterations") is not None:
                    job.iteration = data["loop_iterations"]
                if data.get("applied_files"):
                    job.files_count = len(data["applied_files"])
                job.phase = node_name

                log_line = ""
                if data.get("messages"):
                    log_line = f"[{node_name}] {data['messages'][-1].content[:200]}"
                    job.logs.append(log_line)
                    if len(job.logs) > 100:
                        job.logs = job.logs[-100:]

                final_state.update(data)

                elapsed = int(
                    (datetime.now(timezone.utc) - datetime.fromisoformat(job.started_at))
                    .total_seconds()
                )
                _emit("job_update", {
                    "job_id": job_id,
                    "phase": node_name,
                    "iteration": job.iteration,
                    "files_count": job.files_count,
                    "status": "running",
                    "log": log_line,
                    "elapsed_s": elapsed,
                })

        job.status = "approved" if final_state.get("is_approved") else "rejected"
        job.summary = final_state.get("reviewer_feedback", "")

    except Exception as exc:
        job.status = "error"
        job.summary = str(exc)

    job.finished_at = datetime.now(timezone.utc).isoformat()
    elapsed = int(
        (datetime.now(timezone.utc) - datetime.fromisoformat(job.started_at))
        .total_seconds()
    )
    _emit("job_finished", {
        "job_id": job_id,
        "status": job.status,
        "iterations": job.iteration,
        "files_count": job.files_count,
        "summary": job.summary,
        "elapsed_s": elapsed,
    })

    return {
        "approved": job.status == "approved",
        "iterations": job.iteration,
        "summary": job.summary,
    }
```

- [ ] **Step 4: Ejecutar todos los tests**

```bash
python -m pytest tests/test_fleet_api.py -v
```

Esperado: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add agile_scripts/fleet_api.py tests/test_fleet_api.py
git commit -m "feat: add async fleet worker with SSE event emission"
```

---

## Task 3: Endpoints /run, /status, /status/{job_id}, /health

**Files:**
- Modify: `agile_scripts/fleet_api.py` (agregar endpoints)
- Modify: `tests/test_fleet_api.py`

**Interfaces:**
- Consumes: `JobState`, `_jobs`, `_executor`, `_run_fleet_worker`, `FleetResponse`, `TicketRequest`
- Produces:
  - `POST /run` → `{"job_id": str, "ticket_id": str}` (default) o `FleetResponse` (con `X-Wait: true`)
  - `GET /status` → `Dict[str, dict]`
  - `GET /status/{job_id}` → `dict` (job completo con todos los logs)
  - `GET /health` → `{"status": "ok"}`

---

- [ ] **Step 1: Escribir tests de endpoints**

Agregar al final de `tests/test_fleet_api.py`:

```python
def test_health_endpoint():
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_run_returns_job_id(monkeypatch):
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api
    from fleet_api import app

    # Mock executor para no ejecutar el fleet real
    submitted = []
    def fake_submit(fn, *args):
        submitted.append(args)
        class FakeFuture:
            def result(self): return {"approved": True, "iterations": 1, "summary": "ok"}
        return FakeFuture()

    monkeypatch.setattr(fleet_api._executor, "submit", fake_submit)

    client = TestClient(app)
    r = client.post("/run", json={"ticket_id": "SCRUM-5"})
    assert r.status_code == 200
    body = r.json()
    assert "job_id" in body
    assert body["ticket_id"] == "SCRUM-5"


def test_status_empty():
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api
    fleet_api._jobs.clear()
    from fleet_api import app
    client = TestClient(app)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json() == {}


def test_status_job_id_not_found():
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import app
    client = TestClient(app)
    r = client.get("/status/no-existe")
    assert r.status_code == 404


def test_run_x_wait_returns_fleet_response(monkeypatch):
    """Con X-Wait: true el endpoint espera y retorna FleetResponse."""
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api
    from fleet_api import app

    monkeypatch.setattr(
        fleet_api, "_run_fleet_worker",
        lambda job_id, ticket_id, workspace: {"approved": True, "iterations": 2, "summary": "Listo"}
    )
    # Patch job creation so summary is set before response
    original_worker = fleet_api._run_fleet_worker
    def patched_worker(job_id, ticket_id, workspace):
        result = original_worker(job_id, ticket_id, workspace)
        fleet_api._jobs[job_id].summary = result["summary"]
        fleet_api._jobs[job_id].status = "approved"
        fleet_api._jobs[job_id].iteration = result["iterations"]
        return result
    monkeypatch.setattr(fleet_api, "_run_fleet_worker", patched_worker)

    client = TestClient(app)
    r = client.post("/run", json={"ticket_id": "SCRUM-7"}, headers={"X-Wait": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True
    assert body["ticket_id"] == "SCRUM-7"
    assert "iterations" in body
    assert "summary" in body
```

- [ ] **Step 2: Ejecutar tests — deben fallar**

```bash
python -m pytest tests/test_fleet_api.py::test_health_endpoint tests/test_fleet_api.py::test_run_returns_job_id tests/test_fleet_api.py::test_status_empty -v
```

Esperado: `AttributeError` o `404` — los endpoints no existen aún.

- [ ] **Step 3: Agregar endpoints a fleet_api.py**

Agregar al final de `agile_scripts/fleet_api.py`:

```python
# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/run")
async def run_fleet(req: TicketRequest, request: Request):
    wait = request.headers.get("X-Wait", "").lower() in ("true", "1")
    job_id = str(uuid.uuid4())
    job = JobState(job_id=job_id, ticket_id=req.ticket_id)
    _jobs[job_id] = job

    if wait:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _executor, _run_fleet_worker, job_id, req.ticket_id, req.workspace
        )
        return FleetResponse(
            ticket_id=req.ticket_id,
            approved=result["approved"],
            iterations=result["iterations"],
            summary=result["summary"],
        )

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(
        loop.run_in_executor(_executor, _run_fleet_worker, job_id, req.ticket_id, req.workspace)
    )
    return {"job_id": job_id, "ticket_id": req.ticket_id}


@app.get("/status")
def get_all_status() -> dict:
    return {jid: job.to_dict() for jid, job in _jobs.items()}


@app.get("/status/{job_id}")
def get_job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Job {job_id} no encontrado")
    return job.to_dict()
```

- [ ] **Step 4: Ejecutar todos los tests**

```bash
python -m pytest tests/test_fleet_api.py -v
```

Esperado: `8 passed` (los 3 anteriores + los 5 nuevos).

- [ ] **Step 5: Commit**

```bash
git add agile_scripts/fleet_api.py tests/test_fleet_api.py
git commit -m "feat: add /run async, /status, /health endpoints with X-Wait compat"
```

---

## Task 4: SSE endpoint GET /events

**Files:**
- Modify: `agile_scripts/fleet_api.py` (agregar `GET /events`)
- Modify: `tests/test_fleet_api.py`

**Interfaces:**
- Consumes: `_subscribers`, `_event_queue`
- Produces: `GET /events` → `StreamingResponse` con `Content-Type: text/event-stream`

---

- [ ] **Step 1: Escribir test del SSE endpoint**

Agregar al final de `tests/test_fleet_api.py`:

```python
def test_events_endpoint_content_type():
    """GET /events devuelve Content-Type text/event-stream."""
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import app

    client = TestClient(app, raise_server_exceptions=False)
    with client.stream("GET", "/events") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
```

- [ ] **Step 2: Ejecutar test — debe fallar**

```bash
python -m pytest tests/test_fleet_api.py::test_events_endpoint_content_type -v
```

Esperado: `404` — endpoint no existe.

- [ ] **Step 3: Agregar GET /events a fleet_api.py**

Agregar al final de `agile_scripts/fleet_api.py`:

```python
@app.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(queue)

    async def event_generator():
        # Heartbeat inicial para confirmar conexión al browser
        yield ": connected\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield msg
                except asyncio.TimeoutError:
                    # Keepalive — evita que proxies cierren la conexión idle
                    yield ": keepalive\n\n"
        finally:
            try:
                _subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 4: Ejecutar todos los tests**

```bash
python -m pytest tests/test_fleet_api.py -v
```

Esperado: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add agile_scripts/fleet_api.py tests/test_fleet_api.py
git commit -m "feat: add SSE /events endpoint with keepalive and auto-unsubscribe"
```

---

## Task 5: HTML dashboard en GET / + actualizar mcp_fleet_server.py

**Files:**
- Modify: `agile_scripts/fleet_api.py` (agregar `GET /`, constante `_DASHBOARD_HTML`)
- Modify: `agile_scripts/mcp_fleet_server.py` (agregar header `X-Wait: true`)
- Modify: `tests/test_fleet_api.py`

**Interfaces:**
- Consumes: `GET /events` (EventSource), `GET /status` (carga inicial), `GET /status/{job_id}`
- Produces: `GET /` → `HTMLResponse` con dashboard completo

---

- [ ] **Step 1: Escribir test del dashboard**

Agregar al final de `tests/test_fleet_api.py`:

```python
def test_dashboard_returns_html():
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import app
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "EventSource" in r.text
    assert "/events" in r.text
    assert "/status" in r.text
```

- [ ] **Step 2: Ejecutar test — debe fallar**

```bash
python -m pytest tests/test_fleet_api.py::test_dashboard_returns_html -v
```

Esperado: `404` — `GET /` no existe.

- [ ] **Step 3: Agregar _DASHBOARD_HTML y GET / a fleet_api.py**

Agregar al final de `agile_scripts/fleet_api.py`:

```python
# ---------------------------------------------------------------------------
# Dashboard HTML (inline, sin archivos estáticos)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}
header{background:#1a1f2e;border-bottom:1px solid #2d3748;padding:1rem 2rem;display:flex;align-items:center;gap:.75rem}
header h1{font-size:1.1rem;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:#fc8181;transition:background .3s}
.dot.on{background:#48bb78;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#conn-label{font-size:.75rem;color:#4a5568;margin-left:.25rem}
main{padding:1.5rem;display:grid;gap:1rem;grid-template-columns:repeat(auto-fill,minmax(420px,1fr))}
.empty{text-align:center;color:#4a5568;margin:4rem auto;grid-column:1/-1;font-size:.9rem}
.empty code{background:#1a1f2e;padding:2px 6px;border-radius:4px;font-size:.85rem}
.card{background:#1a1f2e;border:1px solid #2d3748;border-radius:8px;padding:1.25rem;transition:border-color .2s}
.card.running{border-color:#2b4c7e}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.75rem}
.ticket{font-size:1.05rem;font-weight:700;color:#63b3ed}
.meta{display:flex;gap:.75rem;font-size:.75rem;color:#718096;margin-top:.3rem;flex-wrap:wrap;align-items:center}
.badge{font-size:.65rem;font-weight:700;padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.05em}
.badge-running{background:#2b4c7e;color:#63b3ed}
.badge-approved{background:#1c3a2e;color:#48bb78}
.badge-rejected{background:#3a1c1c;color:#fc8181}
.badge-error{background:#3a2a1c;color:#ed8936}
.badge-queued{background:#2d3748;color:#a0aec0}
.phase{display:flex;align-items:center;gap:.3rem}
.pd{width:6px;height:6px;border-radius:50%;background:#718096}
.ph-dynamic_developer .pd{background:#63b3ed}
.ph-quality_reviewer .pd{background:#ecc94b}
.ph-jira_updater .pd{background:#48bb78}
.logs{background:#0f1117;border-radius:4px;padding:.6rem .75rem;font-size:.7rem;font-family:monospace;max-height:100px;overflow-y:auto;color:#a0aec0;cursor:pointer;transition:max-height .2s}
.logs.x{max-height:260px}
.logs-toggle{font-size:.68rem;color:#4a5568;margin-top:.3rem;cursor:pointer;user-select:none}
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>Fleet Dashboard</h1>
  <span id="conn-label">Conectando...</span>
</header>
<main id="grid">
  <div class="empty" id="empty">No hay jobs activos. Inicia uno con <code>POST /run</code>.</div>
</main>
<script>
const grid=document.getElementById('grid'),empty=document.getElementById('empty'),
      dot=document.getElementById('dot'),lbl=document.getElementById('conn-label');
const cards={};
const PHASE_NAMES={context_ingestion:'Contexto',dynamic_developer:'Desarrollando',quality_reviewer:'Revisando',jira_updater:'Actualizando Jira'};

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function elapsed(iso){
  const s=Math.floor((Date.now()-new Date(iso))/1000);
  if(s<60)return s+'s'; if(s<3600)return Math.floor(s/60)+'m '+s%60+'s';
  return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';
}

function buildCard(job){
  const el=document.createElement('div');
  el.className='card'+(job.status==='running'?' running':'');
  el.id='card-'+job.job_id;
  el.innerHTML=cardInner(job);
  el.querySelector('.logs').addEventListener('click',()=>{
    const l=el.querySelector('.logs'),t=el.querySelector('.logs-toggle');
    l.classList.toggle('x');
    t.textContent=l.classList.contains('x')?'▲ colapsar':'▼ expandir';
  });
  return el;
}

function cardInner(job){
  const logLines=(job.logs||[]).slice(-10).map(l=>`<div>${esc(l)}</div>`).join('')||'<div style="color:#4a5568">Sin logs aún.</div>';
  return `<div class="card-header">
    <div>
      <div class="ticket">${esc(job.ticket_id)}</div>
      <div class="meta">
        <span class="phase ph-${job.phase}"><span class="pd"></span>${PHASE_NAMES[job.phase]||job.phase||'—'}</span>
        <span>Ciclo <b id="iter-${job.job_id}">${job.iteration}</b></span>
        <span><b id="files-${job.job_id}">${job.files_count}</b> archivos</span>
        <span class="elapsed" data-started="${job.started_at}" id="elapsed-${job.job_id}">${job.finished_at?elapsed(job.started_at):elapsed(job.started_at)}</span>
      </div>
    </div>
    <span class="badge badge-${job.status}" id="badge-${job.job_id}">${job.status}</span>
  </div>
  <div class="logs" id="logs-${job.job_id}">${logLines}</div>
  <div class="logs-toggle">▼ expandir</div>`;
}

function showCard(job){
  let el=document.getElementById('card-'+job.job_id);
  if(!el){el=buildCard(job);cards[job.job_id]=el;grid.insertBefore(el,grid.firstChild);empty.style.display='none';}
  return el;
}

function patchCard(jobId,d){
  if(d.phase!==undefined){
    const c=document.querySelector(`#card-${jobId} .phase`);
    if(c){c.className=`phase ph-${d.phase}`;c.innerHTML=`<span class="pd"></span>${PHASE_NAMES[d.phase]||d.phase}`;}
  }
  if(d.iteration!==undefined){const e=document.getElementById('iter-'+jobId);if(e)e.textContent=d.iteration;}
  if(d.files_count!==undefined){const e=document.getElementById('files-'+jobId);if(e)e.textContent=d.files_count;}
  if(d.status!==undefined){
    const b=document.getElementById('badge-'+jobId);
    if(b){b.className='badge badge-'+d.status;b.textContent=d.status;}
    const c=document.getElementById('card-'+jobId);
    if(c)c.className='card'+(d.status==='running'?' running':'');
  }
  if(d.log){
    const l=document.getElementById('logs-'+jobId);
    if(l){const div=document.createElement('div');div.textContent=d.log;l.appendChild(div);
      if(l.scrollTop+l.clientHeight>=l.scrollHeight-20)l.scrollTop=l.scrollHeight;}
  }
}

setInterval(()=>{
  document.querySelectorAll('.elapsed[data-started]').forEach(el=>{el.textContent=elapsed(el.dataset.started);});
},1000);

fetch('/status').then(r=>r.json()).then(jobs=>{
  Object.values(jobs).forEach(showCard);
});

function connect(){
  const es=new EventSource('/events');
  es.onopen=()=>{dot.classList.add('on');lbl.textContent='Conectado';};
  es.onerror=()=>{dot.classList.remove('on');lbl.textContent='Reconectando...';};
  es.addEventListener('job_started',e=>{
    const d=JSON.parse(e.data);
    fetch('/status/'+d.job_id).then(r=>r.json()).then(showCard).catch(()=>{});
  });
  es.addEventListener('job_update',e=>{const d=JSON.parse(e.data);patchCard(d.job_id,d);});
  es.addEventListener('job_finished',e=>{const d=JSON.parse(e.data);patchCard(d.job_id,{status:d.status});});
}
connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _DASHBOARD_HTML
```

- [ ] **Step 4: Actualizar mcp_fleet_server.py — agregar X-Wait: true**

Reemplazar en `agile_scripts/mcp_fleet_server.py`:

```python
# Antes:
        response = client.post(
            f"{FLEET_API_URL}/run",
            json={"ticket_id": ticket_id, "workspace": "/workspace"},
        )
        response.raise_for_status()
        result = response.json()

    return (
        f"Ticket: {result['ticket_id']}\n"
        f"Estado: {'✅ Aprobado' if result['approved'] else '⚠️ Máximo de iteraciones alcanzado'}\n"
        f"Ciclos de revisión: {result['iterations']}\n\n"
        f"Resumen:\n{result['summary']}"
    )
```

```python
# Después:
        response = client.post(
            f"{FLEET_API_URL}/run",
            json={"ticket_id": ticket_id, "workspace": "/workspace"},
            headers={"X-Wait": "true"},
        )
        response.raise_for_status()
        result = response.json()

    return (
        f"Ticket: {result['ticket_id']}\n"
        f"Estado: {'✅ Aprobado' if result['approved'] else '⚠️ Máximo de iteraciones alcanzado'}\n"
        f"Ciclos de revisión: {result['iterations']}\n\n"
        f"Resumen:\n{result['summary']}"
    )
```

- [ ] **Step 5: Ejecutar todos los tests**

```bash
python -m pytest tests/test_fleet_api.py -v
```

Esperado: `10 passed`.

- [ ] **Step 6: Rebuild y smoke test del contenedor**

```bash
cd /Users/moyarzun/Documents/Claude/Projects/n8n
docker compose build fleet-api
docker compose restart fleet-api
sleep 5
# Health check
curl -sf http://localhost:8000/health
# Dashboard accesible
curl -sf http://localhost:8000/ | grep -c "EventSource"
# SSE headers
curl -si http://localhost:8000/events --max-time 2 | grep "content-type"
```

Esperado:
```
{"status":"ok"}
1
content-type: text/event-stream; charset=utf-8
```

- [ ] **Step 7: Commit final**

```bash
git add agile_scripts/fleet_api.py agile_scripts/mcp_fleet_server.py tests/test_fleet_api.py
git commit -m "feat: add real-time fleet dashboard with SSE at GET /"
git push origin main
```

---

## Self-Review

**Cobertura del spec:**
- ✅ JobState con todos los campos definidos en el spec
- ✅ `_jobs` + `_subscribers` + broadcast
- ✅ `POST /run` retorna `{job_id}` inmediatamente
- ✅ `POST /run` con `X-Wait: true` retorna `FleetResponse` (compatibilidad MCP)
- ✅ `GET /events` SSE con keepalive y auto-unsubscribe
- ✅ `GET /status` y `GET /status/{job_id}`
- ✅ `GET /` HTML dashboard
- ✅ `GET /health` sin cambios
- ✅ Cleanup de jobs a 1 hora
- ✅ `mcp_fleet_server.py` actualizado con `X-Wait: true`
- ✅ Sin dependencias nuevas

**Tipos consistentes:** `_run_fleet_worker` retorna `dict` con `approved: bool, iterations: int, summary: str` — usado correctamente en `POST /run` modo blocking.

**Sin placeholders:** todo el código de tests e implementación está completo.
