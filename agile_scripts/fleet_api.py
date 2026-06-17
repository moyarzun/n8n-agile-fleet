import os
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from langgraph_fleet import build_architecture, FleetState

app = FastAPI(title="LangGraph Fleet API")


class TicketRequest(BaseModel):
    ticket_id: str
    workspace: str = "/workspace"


class FleetResponse(BaseModel):
    ticket_id: str
    approved: bool
    iterations: int
    summary: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run", response_model=FleetResponse)
def run_fleet(req: TicketRequest):
    engine = build_architecture()
    initial: FleetState = {
        "messages": [],
        "ticket_id": req.ticket_id,
        "workspace_path": req.workspace,
        "acceptance_criteria": "",
        "required_agents": [],
        "current_code_diff": {},
        "applied_files": [],
        "reviewer_feedback": "",
        "is_approved": False,
        "loop_iterations": 0,
    }

    log_lines = []
    final_state = initial.copy()

    for step_event in engine.stream(initial, stream_mode="updates"):
        for node_name, data in step_event.items():
            if data.get("messages"):
                msg = data["messages"][-1].content
                log_lines.append(f"[{node_name}] {msg}")
            final_state.update(data)

    return FleetResponse(
        ticket_id=req.ticket_id,
        approved=final_state.get("is_approved", False),
        iterations=final_state.get("loop_iterations", 0),
        summary=final_state.get("reviewer_feedback", "") or "\n".join(log_lines),
    )
