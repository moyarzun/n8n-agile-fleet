import os
import uuid
import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from langgraph_fleet import build_architecture, FleetState, stop_gracefully, set_log_callback

app = FastAPI(title="LangGraph Fleet API")
_async_executor = ThreadPoolExecutor(max_workers=int(os.getenv("FLEET_ASYNC_WORKERS", "8")))
_wait_executor  = ThreadPoolExecutor(max_workers=int(os.getenv("FLEET_WAIT_WORKERS", "4")))


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
# Persistencia SQLite (comparte volumen n8n_data)
# ---------------------------------------------------------------------------

_DB_PATH = os.getenv("FLEET_DB", "/data/n8n_store/fleet.db")
_db_lock = threading.Lock()


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db_lock:
        conn = _db_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id      TEXT PRIMARY KEY,
                ticket_id   TEXT NOT NULL,
                status      TEXT NOT NULL,
                phase       TEXT DEFAULT '',
                iteration   INTEGER DEFAULT 0,
                files_count INTEGER DEFAULT 0,
                summary     TEXT DEFAULT '',
                started_at  TEXT,
                finished_at TEXT,
                logs        TEXT DEFAULT '[]'
            )
        """)
        conn.commit()
        conn.close()


def _persist_job(job: "JobState") -> None:
    with _db_lock:
        conn = _db_conn()
        conn.execute("""
            INSERT OR REPLACE INTO jobs
              (job_id, ticket_id, status, phase, iteration, files_count,
               summary, started_at, finished_at, logs)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            job.job_id, job.ticket_id, job.status, job.phase,
            job.iteration, job.files_count, job.summary,
            job.started_at, job.finished_at,
            json.dumps(job.logs[-500:]),
        ))
        conn.commit()
        conn.close()


def _load_jobs_from_db() -> Dict[str, "JobState"]:
    jobs: Dict[str, "JobState"] = {}
    try:
        with _db_lock:
            conn = _db_conn()
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY started_at DESC LIMIT 1000"
            ).fetchall()
            conn.close()
        for row in rows:
            j = JobState(job_id=row["job_id"], ticket_id=row["ticket_id"])
            j.status      = row["status"]
            j.phase       = row["phase"] or ""
            j.iteration   = row["iteration"] or 0
            j.files_count = row["files_count"] or 0
            j.summary     = row["summary"] or ""
            j.started_at  = row["started_at"]
            j.finished_at = row["finished_at"]
            j.logs        = json.loads(row["logs"] or "[]")
            jobs[j.job_id] = j
    except Exception as exc:
        print(f"[fleet] Aviso: no se cargaron jobs desde DB: {exc}")
    return jobs


# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------

_jobs: Dict[str, JobState] = {}
_stop_flags: Dict[str, threading.Event] = {}
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
    global _main_loop, _jobs
    _main_loop = asyncio.get_running_loop()
    _init_db()
    _jobs = _load_jobs_from_db()
    print(f"[fleet] DB cargada: {len(_jobs)} jobs restaurados desde {_DB_PATH}")
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


# ---------------------------------------------------------------------------
# Worker — corre en ThreadPoolExecutor
# ---------------------------------------------------------------------------

def _run_fleet_worker(job_id: str, ticket_id: str, workspace: str, stop_flag: threading.Event) -> dict:
    """Ejecuta engine.stream() sincrónicamente. Actualiza _jobs y emite eventos SSE."""
    job = _jobs[job_id]
    job.status = "running"

    _emit("job_started", {
        "job_id": job_id,
        "ticket_id": ticket_id,
        "started_at": job.started_at,
    })

    # Conectar callback de logging verboso al SSE + job.logs
    def _progress_log(message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        job.logs.append(line)
        if len(job.logs) > 500:
            job.logs = job.logs[-500:]
        elapsed = int(
            (datetime.now(timezone.utc) - datetime.fromisoformat(job.started_at))
            .total_seconds()
        )
        _emit("job_update", {
            "job_id": job_id,
            "phase": job.phase,
            "iteration": job.iteration,
            "files_count": job.files_count,
            "status": "running",
            "log": line,
            "elapsed_s": elapsed,
        })
        # Persistir cada 20 líneas para no saturar SQLite
        if len(job.logs) % 20 == 0:
            _persist_job(job)

    set_log_callback(_progress_log)

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
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    log_line = f"[{ts}] [{node_name}] {data['messages'][-1].content[:200]}"
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

            # Chequear flag de parada entre steps (grácil: termina el step actual antes de parar)
            if stop_flag.is_set():
                stop_gracefully(
                    ticket_id=ticket_id,
                    current_phase=job.phase,
                    iterations=job.iteration,
                    applied_files=final_state.get("applied_files", []),
                )
                job.status = "stopped"
                job.summary = "Detenido por el usuario."
                break
        else:
            job.status = "approved" if final_state.get("is_approved") else "rejected"
            job.summary = final_state.get("reviewer_feedback", "")

    except Exception as exc:
        job.status = "error"
        job.summary = str(exc)

    finally:
        _stop_flags.pop(job_id, None)

    job.finished_at = datetime.now(timezone.utc).isoformat()
    _persist_job(job)
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/run")
async def run_fleet(req: TicketRequest, request: Request):
    # Deduplicación: rechazar si el ticket ya tiene un job activo
    active = next(
        (job for job in _jobs.values()
         if job.ticket_id == req.ticket_id and job.status in ("queued", "running")),
        None,
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket {req.ticket_id} ya está siendo procesado (job_id={active.job_id})",
        )

    wait = request.headers.get("X-Wait", "").lower() in ("true", "1")
    job_id = str(uuid.uuid4())
    job = JobState(job_id=job_id, ticket_id=req.ticket_id)
    _jobs[job_id] = job
    _persist_job(job)
    stop_flag = threading.Event()
    _stop_flags[job_id] = stop_flag

    if wait:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _wait_executor, _run_fleet_worker, job_id, req.ticket_id, req.workspace, stop_flag
        )
        return FleetResponse(
            ticket_id=req.ticket_id,
            approved=result["approved"],
            iterations=result["iterations"],
            summary=result["summary"],
        )

    loop = asyncio.get_running_loop()

    async def _fire_and_forget() -> None:
        await loop.run_in_executor(_async_executor, _run_fleet_worker, job_id, req.ticket_id, req.workspace, stop_flag)

    asyncio.create_task(_fire_and_forget())
    return {"job_id": job_id, "ticket_id": req.ticket_id}


@app.post("/stop/{job_id}")
async def stop_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} no encontrado")
    if job.status not in ("queued", "running"):
        raise HTTPException(status_code=409, detail=f"Job {job_id} ya finalizó (status={job.status})")
    flag = _stop_flags.get(job_id)
    if flag:
        flag.set()
    return {"job_id": job_id, "stopping": True}


@app.get("/status")
def get_all_status() -> dict:
    return {jid: job.to_dict() for jid, job in _jobs.items()}


@app.get("/status/{job_id}")
def get_job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} no encontrado")
    return job.to_dict()


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
        except asyncio.CancelledError:
            pass
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
header{background:#1a1f2e;border-bottom:1px solid #2d3748;padding:.65rem 1.1rem;display:flex;align-items:center;gap:.6rem;position:sticky;top:0;z-index:10}
header h1{font-size:.95rem;font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot{width:8px;height:8px;border-radius:50%;background:#fc8181;transition:background .3s;flex-shrink:0}
.dot.on{background:#48bb78;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#conn-label{font-size:.72rem;color:#4a5568;white-space:nowrap}
.btn-hdr{font-size:.72rem;font-weight:600;padding:4px 10px;border-radius:6px;border:1px solid #2d3748;background:#232b3e;color:#a0aec0;cursor:pointer;white-space:nowrap;flex-shrink:0;transition:background .15s,color .15s}
.btn-hdr:hover{background:#2d3748;color:#e2e8f0}
/* ── Layout ── */
main{padding:1rem;display:grid;gap:.85rem;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr))}
.empty{text-align:center;color:#4a5568;margin:4rem auto;grid-column:1/-1;font-size:.9rem}
.empty code{background:#1a1f2e;padding:2px 6px;border-radius:4px;font-size:.85rem}
/* ── Paginación ── */
.pagination{display:flex;align-items:center;gap:.5rem;padding:.6rem 1rem;background:#1a1f2e;border-top:1px solid #2d3748;position:sticky;bottom:0;z-index:9}
.pagination select{background:#0f1117;color:#a0aec0;border:1px solid #2d3748;border-radius:5px;padding:3px 6px;font-size:.72rem;cursor:pointer}
.pagination select:focus{outline:none;border-color:#4a5568}
.pg-btn{background:#232b3e;border:1px solid #2d3748;color:#a0aec0;border-radius:5px;padding:3px 9px;font-size:.8rem;cursor:pointer;transition:background .15s}
.pg-btn:hover:not(:disabled){background:#2d3748;color:#e2e8f0}
.pg-btn:disabled{opacity:.35;cursor:not-allowed}
#page-info{font-size:.72rem;color:#718096;min-width:4rem;text-align:center}
.pg-spacer{flex:1}
/* ── Tarjeta ── */
.card{background:#1a1f2e;border:1px solid #2d3748;border-radius:8px;padding:.9rem;transition:border-color .2s}
.card.running{border-color:#2b4c7e}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;gap:.5rem}
.card-info{min-width:0;flex:1}
.ticket{font-size:.98rem;font-weight:700;color:#63b3ed;word-break:break-word}
.meta{display:flex;gap:.45rem;font-size:.71rem;color:#718096;margin-top:.3rem;flex-wrap:wrap;align-items:center}
.card-body{margin-top:.7rem}
.badge{font-size:.6rem;font-weight:700;padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
.badge-running{background:#2b4c7e;color:#63b3ed}
.badge-approved{background:#1c3a2e;color:#48bb78}
.badge-rejected{background:#3a1c1c;color:#fc8181}
.badge-error{background:#3a2a1c;color:#ed8936}
.badge-queued{background:#2d3748;color:#a0aec0}
.badge-stopped{background:#2d2a3a;color:#b794f4}
.phase{display:flex;align-items:center;gap:.3rem}
.pd{width:6px;height:6px;border-radius:50%;background:#718096;flex-shrink:0}
.ph-dynamic_developer .pd{background:#63b3ed}
.ph-quality_reviewer .pd{background:#ecc94b}
.ph-jira_updater .pd{background:#48bb78}
.logs-preview{background:#0f1117;border-radius:4px;padding:.45rem .6rem;font-size:.67rem;font-family:monospace;max-height:66px;overflow:hidden;color:#a0aec0;line-height:1.4}
.logs-toggle{font-size:.67rem;color:#4a5568;margin-top:.3rem;cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:.25rem}
.logs-toggle:hover{color:#a0aec0}
.spinner{width:18px;height:18px;border:2px solid #2b4c7e;border-top-color:#63b3ed;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.card-status{display:flex;align-items:center;gap:.45rem;flex-shrink:0}
.btn-stop{font-size:.6rem;font-weight:600;padding:3px 8px;border-radius:5px;border:1px solid #4a3060;background:#2d2040;color:#d6bcfa;cursor:pointer;transition:background .15s,opacity .15s;white-space:nowrap}
.btn-stop:hover{background:#3d2a5a}
.btn-stop:disabled{opacity:.45;cursor:not-allowed}
/* ── Vista lista en móvil ── */
@media(max-width:600px){
  main{padding:.5rem;grid-template-columns:1fr;gap:.35rem}
  .card{padding:.65rem .8rem;border-radius:6px}
  .card-body{display:none;margin-top:.55rem;padding-top:.55rem;border-top:1px solid #2d3748}
  .card.expanded .card-body{display:block}
  .card-header{cursor:pointer;-webkit-tap-highlight-color:transparent}
  .ticket{font-size:.9rem}
  .pagination{padding:.5rem .75rem;gap:.4rem}
}
/* ── Modal genérico ── */
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:100;padding:0}
.modal-backdrop.open{display:flex;align-items:stretch}
.modal{background:#1a1f2e;display:flex;flex-direction:column;width:100%;height:100%;overflow:hidden}
@media(min-width:640px){
  .modal-backdrop{padding:2rem;align-items:center}
  .modal{border-radius:10px;max-width:900px;max-height:calc(100vh - 4rem);margin:auto}
}
.modal-header{display:flex;align-items:center;justify-content:space-between;padding:.8rem 1rem;border-bottom:1px solid #2d3748;gap:.6rem;flex-shrink:0}
.modal-title{font-size:.88rem;font-weight:700;color:#63b3ed}
.modal-subtitle{font-size:.7rem;color:#718096;margin-top:.1rem}
.modal-close{background:none;border:none;color:#718096;cursor:pointer;font-size:1.2rem;line-height:1;padding:.2rem .4rem;border-radius:4px;flex-shrink:0}
.modal-close:hover{color:#e2e8f0;background:#2d3748}
.modal-body{flex:1;overflow-y:auto;padding:.7rem .9rem;font-size:.7rem;font-family:monospace;color:#a0aec0;line-height:1.55}
.modal-body .ts{color:#4a5568;user-select:none;margin-right:.35rem}
/* ── Panel "Ver logs" (todas las ejecuciones) ── */
.all-logs-list{display:flex;flex-direction:column;gap:0}
.alr{display:grid;grid-template-columns:1fr auto;align-items:center;gap:.5rem;padding:.6rem .9rem;border-bottom:1px solid #1e2535;cursor:pointer;transition:background .12s}
.alr:hover{background:#212840}
.alr-info{}
.alr-ticket{font-size:.82rem;font-weight:700;color:#63b3ed}
.alr-meta{font-size:.68rem;color:#718096;display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.15rem;align-items:center}
.alr-actions{display:flex;gap:.35rem;flex-shrink:0;align-items:center}
.btn-sm{font-size:.62rem;font-weight:600;padding:2px 7px;border-radius:5px;border:1px solid #2d3748;background:#232b3e;color:#a0aec0;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.2rem;white-space:nowrap}
.btn-sm:hover{background:#2d3748;color:#e2e8f0}
.btn-sm.jira{border-color:#1e3a5f;background:#152840;color:#63b3ed}
.btn-sm.gh{border-color:#1f3324;background:#152216;color:#48bb78}
.alr-expand{background:none;border:none;color:#4a5568;cursor:pointer;font-size:.75rem;padding:.1rem .3rem;border-radius:3px;flex-shrink:0}
.alr-expand:hover{color:#a0aec0;background:#2d3748}
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>Fleet Dashboard</h1>
  <span id="conn-label">Conectando...</span>
  <button class="btn-hdr" onclick="openAllLogs()">📋 Ver logs</button>
</header>
<main id="grid">
  <div class="empty" id="empty">No hay jobs activos. Inicia uno con <code>POST /run</code>.</div>
</main>
<div class="pagination" id="pagination" style="display:none">
  <label style="font-size:.7rem;color:#4a5568">Por página:</label>
  <select id="per-page" onchange="setPerPage(+this.value)">
    <option value="5">5</option>
    <option value="10" selected>10</option>
    <option value="25">25</option>
  </select>
  <div class="pg-spacer"></div>
  <button class="pg-btn" id="prev-btn" onclick="goPage(-1)" disabled>‹</button>
  <span id="page-info">1 / 1</span>
  <button class="pg-btn" id="next-btn" onclick="goPage(1)" disabled>›</button>
</div>

<!-- Modal logs de un job -->
<div class="modal-backdrop" id="modal-backdrop" onclick="closeModalOnBackdrop(event,'modal-backdrop')">
  <div class="modal">
    <div class="modal-header">
      <div>
        <div class="modal-title" id="modal-title">—</div>
        <div class="modal-subtitle" id="modal-subtitle"></div>
      </div>
      <button class="modal-close" onclick="closeLogModal()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<!-- Modal "Ver logs" (todas las ejecuciones) -->
<div class="modal-backdrop" id="all-modal-backdrop" onclick="closeModalOnBackdrop(event,'all-modal-backdrop')">
  <div class="modal" style="max-width:700px">
    <div class="modal-header">
      <div>
        <div class="modal-title">Todas las ejecuciones</div>
        <div class="modal-subtitle" id="all-modal-subtitle"></div>
      </div>
      <button class="modal-close" onclick="closeAllLogs()">✕</button>
    </div>
    <div class="modal-body" style="padding:0;font-family:system-ui,sans-serif">
      <div class="all-logs-list" id="all-logs-list"></div>
    </div>
  </div>
</div>

<script>
const grid=document.getElementById('grid'),empty=document.getElementById('empty'),
      dot=document.getElementById('dot'),lbl=document.getElementById('conn-label');
const cards={};
const pending={};
const jobData={};
const jobLogs={};
const PHASE_NAMES={context_ingestion:'Contexto',dynamic_developer:'Desarrollando',quality_reviewer:'Revisando',jira_updater:'Actualizando Jira'};
const JIRA_BASE='https://veracta.atlassian.net/browse/';
let currentPage=1,perPage=10;

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function elapsed(iso){
  const s=Math.floor((Date.now()-new Date(iso))/1000);
  if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+s%60+'s';
  return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';
}
function findPrUrl(job){
  const text=(job.summary||'')+(jobLogs[job.job_id]||[]).join(' ');
  const m=text.match(/https?:\/\/github\.com\/[^\s"'>]+\/pull\/\d+/);
  return m?m[0]:null;
}

// ── Paginación ──
function getSortedIds(){
  return Object.keys(jobData).sort((a,b)=>new Date(jobData[b].started_at)-new Date(jobData[a].started_at));
}
function renderPage(){
  const ids=getSortedIds();
  const total=ids.length;
  const totalPages=Math.max(1,Math.ceil(total/perPage));
  if(currentPage>totalPages)currentPage=totalPages;
  const start=(currentPage-1)*perPage,end=start+perPage;
  const pageIds=new Set(ids.slice(start,end));
  Object.keys(cards).forEach(jid=>{cards[jid].style.display=pageIds.has(jid)?'':'none';});
  document.getElementById('page-info').textContent=`${currentPage} / ${totalPages}`;
  document.getElementById('prev-btn').disabled=currentPage<=1;
  document.getElementById('next-btn').disabled=currentPage>=totalPages;
  document.getElementById('pagination').style.display=total>0?'flex':'none';
  empty.style.display=total===0?'':'none';
}
function goPage(d){currentPage+=d;renderPage();}
function setPerPage(v){perPage=v;currentPage=1;renderPage();}

// ── Modal logs de un job ──
let _modalJobId=null;
function openLogs(jobId,ticketId){
  _modalJobId=jobId;
  document.getElementById('modal-title').textContent=ticketId;
  document.getElementById('modal-subtitle').textContent='Job '+jobId.slice(0,8)+'…';
  const body=document.getElementById('modal-body');
  body.innerHTML='';
  (jobLogs[jobId]||[]).forEach(l=>body.appendChild(makeLogLine(l)));
  body.scrollTop=body.scrollHeight;
  document.getElementById('modal-backdrop').classList.add('open');
  document.body.style.overflow='hidden';
}
function closeLogModal(){
  document.getElementById('modal-backdrop').classList.remove('open');
  document.body.style.overflow='';
  _modalJobId=null;
}

// ── Modal "Ver logs" ──
function openAllLogs(){
  const list=document.getElementById('all-logs-list');
  list.innerHTML='';
  const sorted=getSortedIds();
  document.getElementById('all-modal-subtitle').textContent=`${sorted.length} ejecuciones`;
  sorted.forEach(jid=>{
    const job=jobData[jid];
    if(!job)return;
    const pr=findPrUrl(job);
    const prBtn=pr?`<a class="btn-sm gh" href="${esc(pr)}" target="_blank" rel="noopener">⑁ PR</a>`:'';
    const statusBadge=job.status==='running'
      ?`<div class="spinner" style="width:14px;height:14px;border-width:2px"></div>`
      :`<span class="badge badge-${job.status}">${job.status}</span>`;
    const row=document.createElement('div');
    row.className='alr';
    row.innerHTML=`
      <div class="alr-info">
        <div class="alr-ticket">${esc(job.ticket_id)}</div>
        <div class="alr-meta">
          ${statusBadge}
          <span class="phase ph-${job.phase}" style="display:inline-flex;align-items:center;gap:.25rem"><span class="pd"></span>${PHASE_NAMES[job.phase]||job.phase||'—'}</span>
          <span>Ciclo ${job.iteration}</span>
          <span class="elapsed" data-started="${job.started_at}">${elapsed(job.started_at)}</span>
        </div>
      </div>
      <div class="alr-actions">
        <a class="btn-sm jira" href="${JIRA_BASE}${esc(job.ticket_id)}" target="_blank" rel="noopener">Jira ↗</a>
        ${prBtn}
        <button class="btn-sm" onclick="openLogs('${jid}','${esc(job.ticket_id)}')">📄 Logs</button>
      </div>`;
    list.appendChild(row);
  });
  document.getElementById('all-modal-backdrop').classList.add('open');
  document.body.style.overflow='hidden';
}
function closeAllLogs(){
  document.getElementById('all-modal-backdrop').classList.remove('open');
  if(!document.getElementById('modal-backdrop').classList.contains('open'))
    document.body.style.overflow='';
}
function closeModalOnBackdrop(e,id){if(e.target===document.getElementById(id)){
  document.getElementById(id).classList.remove('open');
  if(!document.getElementById('modal-backdrop').classList.contains('open')&&
     !document.getElementById('all-modal-backdrop').classList.contains('open'))
    document.body.style.overflow='';
}}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(document.getElementById('modal-backdrop').classList.contains('open'))closeLogModal();
    else if(document.getElementById('all-modal-backdrop').classList.contains('open'))closeAllLogs();
  }
});

function makeLogLine(l){
  const div=document.createElement('div');
  const m=l.match(/^(\[\d{2}:\d{2}:\d{2}\])\s(.+)$/);
  if(m){const ts=document.createElement('span');ts.className='ts';ts.textContent=m[1];div.appendChild(ts);div.appendChild(document.createTextNode(m[2]));}
  else{div.textContent=l;}
  return div;
}

// ── Tarjeta ──
function buildCard(job){
  const el=document.createElement('div');
  el.className='card'+(job.status==='running'?' running':'');
  el.id='card-'+job.job_id;
  el.innerHTML=cardInner(job);
  // Móvil: tap en header expande/colapsa
  el.querySelector('.card-header').addEventListener('click',()=>{
    if(window.innerWidth<=600)el.classList.toggle('expanded');
  });
  el.querySelector('.logs-toggle').addEventListener('click',e=>{
    e.stopPropagation();openLogs(job.job_id,job.ticket_id);
  });
  return el;
}

function cardInner(job){
  const preview=(job.logs||[]).slice(-3).map(l=>{
    const m=l.match(/^(\[\d{2}:\d{2}:\d{2}\])\s(.+)$/);
    return`<div>${m?`<span style="color:#4a5568">${esc(m[1])} </span>${esc(m[2])}`:esc(l)}</div>`;
  }).join('')||'<div style="color:#4a5568">Sin logs aún.</div>';
  const isRunning=job.status==='running';
  const statusEl=isRunning
    ?`<div class="spinner" id="badge-${job.job_id}"></div>`
    :`<span class="badge badge-${job.status}" id="badge-${job.job_id}">${job.status}</span>`;
  const stopBtn=isRunning
    ?`<button class="btn-stop" id="stop-${job.job_id}" onclick="event.stopPropagation();stopJob('${job.job_id}')">⏹ Detener</button>`
    :'';
  return `<div class="card-header">
    <div class="card-info">
      <div class="ticket">${esc(job.ticket_id)}</div>
      <div class="meta">
        <span class="phase ph-${job.phase}" id="phase-${job.job_id}"><span class="pd"></span>${PHASE_NAMES[job.phase]||job.phase||'—'}</span>
        <span>Ciclo <b id="iter-${job.job_id}">${job.iteration}</b></span>
        <span><b id="files-${job.job_id}">${job.files_count}</b> arch.</span>
        <span class="elapsed" data-started="${job.started_at}" id="elapsed-${job.job_id}">${elapsed(job.started_at)}</span>
      </div>
    </div>
    <div class="card-status">${stopBtn}${statusEl}</div>
  </div>
  <div class="card-body">
    <div class="logs-preview" id="logs-${job.job_id}">${preview}</div>
    <div class="logs-toggle" id="toggle-${job.job_id}">▼ ver logs completos</div>
  </div>`;
}

function showCard(job){
  jobData[job.job_id]=job;
  jobLogs[job.job_id]=job.logs||[];
  let el=document.getElementById('card-'+job.job_id);
  if(!el){el=buildCard(job);cards[job.job_id]=el;grid.insertBefore(el,grid.firstChild);}
  renderPage();
  return el;
}

function patchCard(jobId,d){
  if(d.phase!==undefined){
    const c=document.getElementById('phase-'+jobId);
    if(c){c.className=`phase ph-${d.phase}`;c.innerHTML=`<span class="pd"></span>${PHASE_NAMES[d.phase]||d.phase}`;}
    if(jobData[jobId])jobData[jobId].phase=d.phase;
  }
  if(d.iteration!==undefined){
    const e=document.getElementById('iter-'+jobId);if(e)e.textContent=d.iteration;
    if(jobData[jobId])jobData[jobId].iteration=d.iteration;
  }
  if(d.files_count!==undefined){
    const e=document.getElementById('files-'+jobId);if(e)e.textContent=d.files_count;
    if(jobData[jobId])jobData[jobId].files_count=d.files_count;
  }
  if(d.status!==undefined){
    const b=document.getElementById('badge-'+jobId);
    if(b){
      if(d.status==='running'){b.className='spinner';b.textContent='';}
      else{b.className='badge badge-'+d.status;b.textContent=d.status;
        const btn=document.getElementById('stop-'+jobId);if(btn)btn.remove();}
    }
    const c=document.getElementById('card-'+jobId);
    if(c)c.className='card'+(d.status==='running'?' running':'');
    if(jobData[jobId])jobData[jobId].status=d.status;
  }
  if(d.log){
    if(!jobLogs[jobId])jobLogs[jobId]=[];
    jobLogs[jobId].push(d.log);
    const p=document.getElementById('logs-'+jobId);
    if(p){
      const recent=jobLogs[jobId].slice(-3);
      p.innerHTML=recent.map(l=>{
        const m=l.match(/^(\[\d{2}:\d{2}:\d{2}\])\s(.+)$/);
        return`<div>${m?`<span style="color:#4a5568">${esc(m[1])} </span>${esc(m[2])}`:esc(l)}</div>`;
      }).join('');
    }
    if(_modalJobId===jobId){
      const body=document.getElementById('modal-body');
      const atBottom=body.scrollTop+body.clientHeight>=body.scrollHeight-30;
      body.appendChild(makeLogLine(d.log));
      if(atBottom)body.scrollTop=body.scrollHeight;
    }
  }
}

setInterval(()=>{
  document.querySelectorAll('.elapsed[data-started]').forEach(el=>{el.textContent=elapsed(el.dataset.started);});
},1000);

fetch('/status').then(r=>r.json()).then(jobs=>{
  Object.values(jobs).forEach(showCard);
});

function stopJob(jobId){
  const btn=document.getElementById('stop-'+jobId);
  if(btn){btn.disabled=true;btn.textContent='Deteniendo…';}
  fetch('/stop/'+jobId,{method:'POST'})
    .then(r=>{if(!r.ok)throw new Error(r.status);})
    .catch(()=>{if(btn){btn.disabled=false;btn.textContent='⏹ Detener';}});
}

function connect(){
  const es=new EventSource('/events');
  es.onopen=()=>{dot.classList.add('on');lbl.textContent='Conectado';};
  es.onerror=()=>{dot.classList.remove('on');lbl.textContent='Reconectando...';};
  es.addEventListener('job_started',e=>{
    const d=JSON.parse(e.data);pending[d.job_id]=[];
    fetch('/status/'+d.job_id).then(r=>r.json()).then(job=>{
      showCard(job);
      (pending[d.job_id]||[]).forEach(u=>patchCard(u.job_id,u));
      delete pending[d.job_id];
    }).catch(()=>{});
  });
  es.addEventListener('job_update',e=>{const d=JSON.parse(e.data);if(!cards[d.job_id]){if(pending[d.job_id])pending[d.job_id].push(d);return;}patchCard(d.job_id,d);});
  es.addEventListener('job_finished',e=>{const d=JSON.parse(e.data);patchCard(d.job_id,{status:d.status});});
}
connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _DASHBOARD_HTML
