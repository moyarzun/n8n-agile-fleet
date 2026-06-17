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
