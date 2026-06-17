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
