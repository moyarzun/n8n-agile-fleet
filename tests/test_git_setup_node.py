"""
Regresión para el requerimiento "git_setup_node debe usar la rama actualmente
checked out como base, no siempre develop/main" (ver
requerimientos/03-git-setup-usa-siempre-develop-o-main.md).

conftest.py reemplaza sys.modules["langgraph_fleet"] por un mock (para que
test_fleet_api.py pueda importar fleet_api sin instalar langgraph/langchain
completos). Acá necesitamos el módulo REAL para ejercitar git_setup_node, así
que lo cargamos por ruta de archivo bajo un nombre distinto, stubbeando solo
sus dependencias externas pesadas (langgraph, langchain, openai, atlassian).
"""
import importlib.util
import os
import subprocess
import sys
import types

import pytest


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _load_real_langgraph_fleet():
    _stub_module("langchain_core")
    _stub_module(
        "langchain_core.messages",
        BaseMessage=object,
        HumanMessage=lambda *a, **k: None,
        SystemMessage=lambda *a, **k: None,
        AIMessage=lambda *a, **k: types.SimpleNamespace(content=k.get("content"), name=k.get("name")),
    )
    _stub_module("langgraph")
    _stub_module(
        "langgraph.graph",
        StateGraph=object,
        START="START",
        END="END",
    )
    _stub_module("langgraph.graph.message", add_messages=lambda *a, **k: None)
    class _FakeChatOpenAI:
        def __init__(self, *args, **kwargs):
            pass

    _stub_module("langchain_openai", ChatOpenAI=_FakeChatOpenAI)
    _stub_module("openai", RateLimitError=Exception, APIStatusError=Exception)
    _stub_module("atlassian", Jira=object)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    module_path = os.path.join(repo_root, "agile_scripts", "langgraph_fleet.py")
    spec = importlib.util.spec_from_file_location("langgraph_fleet_real", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_git_setup_node")


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


def _state(workspace, ticket="TASK-1", title="Fix de prueba"):
    return {
        "workspace_path": str(workspace),
        "ticket_id": ticket,
        "acceptance_criteria": f"TITULO: {title}\nresto",
    }


def test_usa_la_rama_feature_checked_out_como_base(real_fleet, tmp_path):
    repo = _make_repo(tmp_path)

    _run_git(["checkout", "-b", "feature/mi-cambio"], repo)
    (repo / "plan.md").write_text("plan especifico de la rama feature\n")
    _run_git(["add", "plan.md"], repo)
    _run_git(["commit", "-m", "agrega plan de la rama feature"], repo)

    result = real_fleet.git_setup_node(_state(repo, ticket="TASK-42"))

    assert not result.get("aborted")
    assert result["base_branch"] == "feature/mi-cambio"

    worktree_path = result["workspace_path"]
    assert os.path.exists(os.path.join(worktree_path, "plan.md")), (
        "el worktree debe partir de la rama feature checked out, no de main: "
        "el archivo propio de esa rama debe existir"
    )

    merge_base = subprocess.run(
        ["git", "merge-base", "feature/mi-cambio", result["work_branch"]],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    feature_head = subprocess.run(
        ["git", "rev-parse", "feature/mi-cambio"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert merge_base == feature_head


def test_head_detached_cae_a_comportamiento_anterior(real_fleet, tmp_path):
    repo = _make_repo(tmp_path)

    main_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _run_git(["checkout", main_head], repo)  # HEAD detached

    result = real_fleet.git_setup_node(_state(repo, ticket="TASK-7"))

    assert not result.get("aborted")
    assert result["base_branch"] == "main"


def test_en_main_sin_rama_feature_sigue_resolviendo_a_main(real_fleet, tmp_path):
    repo = _make_repo(tmp_path)

    result = real_fleet.git_setup_node(_state(repo, ticket="TASK-9"))

    assert not result.get("aborted")
    assert result["base_branch"] == "main"


def test_npm_install_falla_dos_veces_aborta_git_setup(real_fleet, tmp_path, monkeypatch):
    """Regresión requerimiento 04: si `npm install` falla dos veces (fallo +
    reintento), git_setup_node debe abortar en vez de continuar con
    dependencias parcialmente instaladas."""
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text('{"name": "demo", "version": "1.0.0"}\n')
    _run_git(["add", "package.json"], repo)
    _run_git(["commit", "-m", "agrega package.json"], repo)

    real_run = real_fleet._run
    npm_install_calls = []

    def fake_run(cmd, cwd, timeout=120):
        if cmd[:2] == ["npm", "install"]:
            npm_install_calls.append(cmd)
            return 1, "npm ERR! network timeout"
        return real_run(cmd, cwd, timeout=timeout)

    monkeypatch.setattr(real_fleet, "_run", fake_run)

    result = real_fleet.git_setup_node(_state(repo, ticket="TASK-99"))

    assert result.get("aborted") is True
    assert "npm install" in result["reviewer_feedback"]
    assert len(npm_install_calls) == 2  # intento inicial + un reintento, no más

    worktree_root = tmp_path / ".fleet-worktrees"
    assert not any(worktree_root.iterdir()) if worktree_root.exists() else True


def test_npm_install_falla_una_vez_pero_reintento_ok_continua(real_fleet, tmp_path, monkeypatch):
    """Si el reintento de npm install tiene éxito, el pipeline sigue normalmente."""
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text('{"name": "demo", "version": "1.0.0"}\n')
    _run_git(["add", "package.json"], repo)
    _run_git(["commit", "-m", "agrega package.json"], repo)

    real_run = real_fleet._run
    npm_install_calls = []

    def fake_run(cmd, cwd, timeout=120):
        if cmd[:2] == ["npm", "install"]:
            npm_install_calls.append(cmd)
            if len(npm_install_calls) == 1:
                return 1, "npm ERR! network timeout"
            return 0, "up to date"
        return real_run(cmd, cwd, timeout=timeout)

    monkeypatch.setattr(real_fleet, "_run", fake_run)

    result = real_fleet.git_setup_node(_state(repo, ticket="TASK-100"))

    assert not result.get("aborted")
    assert len(npm_install_calls) == 2
