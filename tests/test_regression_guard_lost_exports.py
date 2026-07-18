"""
Requerimiento 16: un ciclo reescribió un route.ts bajo ALLOW_REWRITE y perdió
el handler GET; regression_guard no lo detectó porque codebase_reader nunca
capturó el original del archivo (ruta dinámica de Next.js con corchetes, que
la regex de paths no matcheaba). Fix: regression_guard obtiene el 'antes'
desde git (HEAD del worktree) para cualquier archivo aplicado.
"""
import os
import subprocess

import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_lost_exports")


# ---------------------------------------------------------------------------
# _check_ts_exports_regression — detección de handlers HTTP perdidos
# ---------------------------------------------------------------------------

def test_detecta_handler_GET_perdido_en_route_ts(real_fleet):
    old = (
        "export async function GET(req) { return list(); }\n"
        "export async function POST(req) { return create(); }\n"
    )
    new = "export async function POST(req) { return create(); }\n"  # GET desaparece

    issues = real_fleet._check_ts_exports_regression(old, new)

    assert len(issues) == 1
    assert "GET" in issues[0]
    assert "HTTP" in issues[0]  # se marca como handler HTTP / endpoint perdido


def test_no_marca_regresion_si_se_conservan_los_exports(real_fleet):
    old = "export async function GET(req) { return a(); }\n"
    new = "export async function GET(req) { return a(); }\nexport const dynamic = 'force-dynamic';\n"

    assert real_fleet._check_ts_exports_regression(old, new) == []


# ---------------------------------------------------------------------------
# regression_guard con git real — reproduce el caso exacto (criterio 2)
# ---------------------------------------------------------------------------

def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-b", "main"], repo)
    _run_git(["config", "user.email", "t@t.local"], repo)
    _run_git(["config", "user.name", "T"], repo)
    return repo


def test_regression_guard_detecta_GET_perdido_via_git_en_ruta_dinamica(real_fleet, tmp_path):
    """Criterio de aceptación 2: un route.ts (ruta DINÁMICA con corchetes) con
    GET y POST es reescrito dejando solo POST → regression_guard debe marcarlo
    como regresión y restaurar, aunque codebase_reader NO lo haya capturado
    (existing_files vacío) — el 'antes' se recupera desde git HEAD."""
    repo = _make_repo(tmp_path)
    route_rel = "src/app/api/messages/[conversationId]/route.ts"
    route_abs = repo / route_rel
    route_abs.parent.mkdir(parents=True)
    original = (
        "export async function GET(req, { params }) {\n"
        "  return listMessages(params.conversationId);\n"
        "}\n"
        "export async function POST(req, { params }) {\n"
        "  return markRead(params.conversationId);\n"
        "}\n"
    )
    route_abs.write_text(original)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-m", "ruta original con GET y POST"], repo)

    # dynamic_developer reescribió el archivo dejando solo POST (uncommitted).
    truncated = (
        "export async function POST(req, { params }) {\n"
        "  return markRead(params.conversationId);\n"
        "}\n"
    )
    route_abs.write_text(truncated)

    # codebase_reader NO capturó la ruta dinámica → existing_files vacío.
    state = {
        "workspace_path": str(repo),
        "existing_files": {},
        "applied_files": [route_rel],
    }

    result = real_fleet.regression_guard_node(state)

    assert result["regression_errors"], "debe detectar la pérdida del GET vía git"
    assert any("GET" in e for e in result["regression_errors"])
    # el archivo fue restaurado a su versión original (con GET) — la versión
    # truncada NO quedó en disco.
    assert route_abs.read_text() == original


def test_regression_guard_no_marca_archivo_nuevo_del_ticket(real_fleet, tmp_path):
    """Caso negativo: un archivo que NO existía en HEAD (nuevo del ticket) no
    tiene 'antes' en git → no puede ser regresión, no se marca."""
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("x\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-m", "base"], repo)

    nuevo_rel = "src/server/messages/message-service.ts"
    nuevo_abs = repo / nuevo_rel
    nuevo_abs.parent.mkdir(parents=True)
    nuevo_abs.write_text("export function svc() {}\n")

    state = {
        "workspace_path": str(repo),
        "existing_files": {},
        "applied_files": [nuevo_rel],
    }

    result = real_fleet.regression_guard_node(state)
    assert result["regression_errors"] == []


def test_codebase_reader_regex_captura_ruta_dinamica_con_corchetes(real_fleet):
    """Fix de prevención: la extracción de rutas ahora captura `[param]` de
    Next.js, para que codebase_reader lea el original y el modelo lo vea."""
    paths = real_fleet._mentioned_file_paths(
        "Migra src/app/api/messages/[conversationId]/route.ts al nuevo patrón."
    )
    assert "src/app/api/messages/[conversationId]/route.ts" in paths
