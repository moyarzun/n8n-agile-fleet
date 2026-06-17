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
