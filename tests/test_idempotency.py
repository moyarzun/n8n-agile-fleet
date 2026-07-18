"""
Spec langgraph-hardening — Req 5 (idempotencia de nodos ante reanudación).

5.1: git_setup_node re-ejecutado con worktree preexistente.
5.4: git_finalize_node re-ejecutado con árbol limpio (nada que commitear).
El caso 5.2/5.3 (@task por agent_role) vive en test_checkpointer_real.py
porque necesita el langgraph real.
"""
import os
import subprocess

import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_idempotency")


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("base\n")
    _run_git(["add", "README.md"], repo)
    _run_git(["commit", "-m", "initial"], repo)
    return repo


def test_git_setup_re_ejecutado_con_worktree_existente_no_lanza(real_fleet, tmp_path):
    """Req 5.1: la re-ejecución del nodo para el mismo ticket recrea el
    worktree sin excepción (formaliza el comportamiento ya implementado)."""
    repo = _make_repo(tmp_path)
    state = {
        "workspace_path": str(repo),
        "ticket_id": "TASK-RE",
        "acceptance_criteria": "TITULO: idempotencia\nresto",
    }

    first = real_fleet.git_setup_node(state)
    assert not first.get("aborted")
    assert os.path.isdir(first["workspace_path"])

    second = real_fleet.git_setup_node(state)  # re-ejecución (reanudación)
    assert not second.get("aborted")
    assert os.path.isdir(second["workspace_path"])
    assert second["work_branch"] == first["work_branch"]


def test_git_finalize_con_arbol_limpio_no_crea_commit_ni_lanza(real_fleet, tmp_path):
    """Req 5.4: re-ejecutar git_finalize con todo ya commiteado no debe crear
    un commit vacío ni lanzar excepción."""
    repo = _make_repo(tmp_path)
    _run_git(["checkout", "-b", "fleet/TASK-X-cambios"], repo)

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()

    state = {
        "workspace_path": str(repo),
        "ticket_id": "TASK-X",
        "work_branch": "fleet/TASK-X-cambios",
        "is_approved": True,
        "validation_passed": True,
        "applied_files": ["README.md"],  # ya commiteado en una ejecución previa
        "acceptance_criteria": "TITULO: cambios\nresto",
        "reviewer_feedback": "ok",
        "loop_iterations": 1,
    }

    result = real_fleet.git_finalize_node(state)  # no debe lanzar

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head_after == head_before  # sin commit vacío
    assert "pr_url" in result  # el nodo completó su contrato de salida
