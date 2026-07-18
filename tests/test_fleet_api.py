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
        "job_id", "ticket_id", "workspace", "status", "phase",
        "iteration", "files_count", "logs",
        "started_at", "finished_at", "summary",
    }
    assert d["logs"] == []


def test_run_fleet_worker_updates_job_state(monkeypatch):
    """Worker actualiza status y emite eventos correctamente."""
    import sys, os, threading
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api

    job = fleet_api.JobState(job_id="test-1", ticket_id="SCRUM-99")
    fleet_api._jobs["test-1"] = job

    emitted = []
    monkeypatch.setattr(fleet_api, "_emit", lambda name, data: emitted.append((name, data)))

    # Mock engine que emite un evento developer y uno reviewer con aprobación
    class FakeMsg:
        content = "Test message"

    def fake_stream(initial, config, stream_mode):
        yield {"dynamic_developer": {"messages": [FakeMsg()], "loop_iterations": 1, "applied_files": ["a.py", "b.py"]}}
        yield {"quality_reviewer": {"messages": [FakeMsg()], "is_approved": True, "reviewer_feedback": "OK"}}

    class FakeEngine:
        def stream(self, initial, config=None, stream_mode=None):
            return fake_stream(initial, config, stream_mode)

    monkeypatch.setattr(fleet_api, "build_architecture", lambda: FakeEngine())

    result = fleet_api._run_fleet_worker("test-1", "SCRUM-99", "/workspace", threading.Event())

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

    # Mock _run_fleet_worker para no ejecutar el fleet real
    monkeypatch.setattr(
        fleet_api, "_run_fleet_worker",
        lambda job_id, ticket_id, workspace, stop_flag: {"approved": True, "iterations": 1, "summary": "ok"}
    )

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
        lambda job_id, ticket_id, workspace, stop_flag: {"approved": True, "iterations": 2, "summary": "Listo"}
    )

    client = TestClient(app)
    r = client.post("/run", json={"ticket_id": "SCRUM-7"}, headers={"X-Wait": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True
    assert body["ticket_id"] == "SCRUM-7"
    assert "iterations" in body
    assert "summary" in body


def test_events_endpoint_content_type():
    """GET /events devuelve Content-Type text/event-stream."""
    import sys, os, threading, time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import app
    import uvicorn, httpx

    # Arranca servidor real en thread para soportar SSE infinito
    config = uvicorn.Config(app, host="127.0.0.1", port=18765, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Esperar hasta que el servidor esté listo
    for _ in range(20):
        try:
            httpx.get("http://127.0.0.1:18765/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    try:
        with httpx.stream("GET", "http://127.0.0.1:18765/events", timeout=5) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
    finally:
        server.should_exit = True
        thread.join(timeout=3)


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


# ---------------------------------------------------------------------------
# Spec langgraph-hardening — Req 2 (reanudación) y Req 7.2 (GraphRecursionError)
# ---------------------------------------------------------------------------

def _make_interrupted_job(fleet_api, job_id="int-1", ticket_id="SCRUM-77"):
    job = fleet_api.JobState(job_id=job_id, ticket_id=ticket_id, workspace="/workspace")
    job.status = "interrupted"
    job.finished_at = None
    fleet_api._jobs[job_id] = job
    return job


def test_resume_endpoint_404_si_job_no_existe():
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    from fleet_api import app
    client = TestClient(app)
    r = client.post("/resume/no-existe")
    assert r.status_code == 404


def test_resume_endpoint_409_si_status_no_es_interrupted():
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api
    from fleet_api import app

    job = fleet_api.JobState(job_id="done-1", ticket_id="SCRUM-1", workspace="/workspace")
    job.status = "approved"
    fleet_api._jobs["done-1"] = job

    client = TestClient(app)
    r = client.post("/resume/done-1")
    assert r.status_code == 409


def test_resume_endpoint_reanuda_job_interrumpido(monkeypatch):
    from starlette.testclient import TestClient
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api
    from fleet_api import app

    _make_interrupted_job(fleet_api, job_id="int-2")

    calls = []
    monkeypatch.setattr(
        fleet_api, "_run_fleet_worker",
        lambda job_id, ticket_id, workspace, stop_flag, requirement="", agents=None, resume=False:
            calls.append({"job_id": job_id, "resume": resume}) or {"approved": True},
    )

    client = TestClient(app)
    r = client.post("/resume/int-2")
    assert r.status_code == 200
    assert r.json() == {"job_id": "int-2", "resuming": True}
    assert fleet_api._jobs["int-2"].status == "running"


def test_auto_resume_respeta_env_var(monkeypatch):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api

    calls = []
    monkeypatch.setattr(
        fleet_api, "_run_fleet_worker",
        lambda *a, **kw: calls.append(kw) or {"approved": True},
    )

    # Sin FLEET_AUTO_RESUME: no reanuda nada (Req 2.3)
    monkeypatch.delenv("FLEET_AUTO_RESUME", raising=False)
    _make_interrupted_job(fleet_api, job_id="int-3")
    assert fleet_api._auto_resume_interrupted() == 0
    assert fleet_api._jobs["int-3"].status == "interrupted"

    # Con FLEET_AUTO_RESUME=true: despacha worker con resume=True (Req 2.2)
    monkeypatch.setenv("FLEET_AUTO_RESUME", "true")
    assert fleet_api._auto_resume_interrupted() == 1
    assert fleet_api._jobs["int-3"].status == "running"


def test_graph_recursion_error_marca_job_error_con_summary_claro(monkeypatch):
    """Req 7.2: GraphRecursionError → status 'error' + summary que menciona el límite."""
    import sys, os, threading
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api

    job = fleet_api.JobState(job_id="rec-1", ticket_id="SCRUM-9", workspace="/workspace")
    fleet_api._jobs["rec-1"] = job
    monkeypatch.setattr(fleet_api, "_emit", lambda name, data: None)

    class RecursionEngine:
        def stream(self, initial, config=None, stream_mode=None):
            raise fleet_api.GraphRecursionError("limit hit")

    monkeypatch.setattr(fleet_api, "build_architecture", lambda: RecursionEngine())

    fleet_api._run_fleet_worker("rec-1", "SCRUM-9", "/workspace", threading.Event())

    assert job.status == "error"
    assert "recursión" in job.summary.lower()


def test_worker_tolera_eventos_de_task_con_valor_no_dict(monkeypatch):
    """Regresión (bug encontrado en la prueba de humo de la spec): los @task
    dentro de nodos emiten eventos en stream_mode='updates' con su valor de
    retorno crudo (str) — el worker no debe crashear con
    "'str' object has no attribute 'get'"."""
    import sys, os, threading
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api

    job = fleet_api.JobState(job_id="task-ev-1", ticket_id="SCRUM-3", workspace="/workspace")
    fleet_api._jobs["task-ev-1"] = job
    monkeypatch.setattr(fleet_api, "_emit", lambda name, data: None)

    class FakeMsg:
        content = "ok"

    class TaskEventEngine:
        def stream(self, initial, config=None, stream_mode=None):
            yield {"_agent_generation": "===FILE_BEGIN: a.py===\ncodigo\n===FILE_END==="}  # evento de task (str)
            yield {"quality_reviewer": {"messages": [FakeMsg()], "is_approved": True, "reviewer_feedback": "OK"}}

    monkeypatch.setattr(fleet_api, "build_architecture", lambda: TaskEventEngine())

    fleet_api._run_fleet_worker("task-ev-1", "SCRUM-3", "/workspace", threading.Event())

    assert job.status == "approved"


def test_worker_borra_checkpoints_al_estado_final(monkeypatch):
    """Req 1.3: al alcanzar estado final, se eliminan los checkpoints del thread."""
    import sys, os, threading
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))
    import fleet_api

    job = fleet_api.JobState(job_id="ck-1", ticket_id="SCRUM-5", workspace="/workspace")
    fleet_api._jobs["ck-1"] = job
    monkeypatch.setattr(fleet_api, "_emit", lambda name, data: None)

    deleted = []
    monkeypatch.setattr(fleet_api, "delete_job_checkpoints", lambda job_id: deleted.append(job_id))

    class FakeMsg:
        content = "ok"

    class DoneEngine:
        def stream(self, initial, config=None, stream_mode=None):
            yield {"quality_reviewer": {"messages": [FakeMsg()], "is_approved": True, "reviewer_feedback": "OK"}}

    monkeypatch.setattr(fleet_api, "build_architecture", lambda: DoneEngine())

    fleet_api._run_fleet_worker("ck-1", "SCRUM-5", "/workspace", threading.Event())

    assert job.status == "approved"
    assert deleted == ["ck-1"]
