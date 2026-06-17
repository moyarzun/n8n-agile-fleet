import os
import uuid
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from langgraph_fleet import build_architecture, FleetState

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
    _main_loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _wait_executor, _run_fleet_worker, job_id, req.ticket_id, req.workspace
        )
        return FleetResponse(
            ticket_id=req.ticket_id,
            approved=result["approved"],
            iterations=result["iterations"],
            summary=result["summary"],
        )

    loop = asyncio.get_running_loop()

    async def _fire_and_forget() -> None:
        await loop.run_in_executor(_async_executor, _run_fleet_worker, job_id, req.ticket_id, req.workspace)

    asyncio.create_task(_fire_and_forget())
    return {"job_id": job_id, "ticket_id": req.ticket_id}


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
const pending={};
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
    pending[d.job_id]=[];
    fetch('/status/'+d.job_id).then(r=>r.json()).then(job=>{
      const el=showCard(job);
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
