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
        lambda job_id, ticket_id, workspace: {"approved": True, "iterations": 1, "summary": "ok"}
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
