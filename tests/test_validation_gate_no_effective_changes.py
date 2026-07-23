"""
Requerimiento 20: la Flota reportaba "Aprobado+validado" en un ticket que no
produjo ningún cambio real. Caso real: TASK-1f936d99 le pedía a la Flota
ejecutar un `git merge` con resolución de conflicto — operación que el
pipeline no sabe ejecutar (solo escribe diffs de archivo) — y el LLM devolvió
el archivo tal cual estaba. Los mismos tests que ya pasaban siguieron
pasando, y el ciclo terminó aprobado sin ningún commit real.

Fix: validation_gate_node rechaza explícitamente cualquier ciclo donde todos
los archivos "aplicados" sean byte-idénticos a su contenido previo.
"""
import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_no_effective_changes")


def test_no_effective_changes_true_si_contenido_identico(real_fleet, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rel = "src/foo.ts"
    full = workspace / rel
    full.parent.mkdir(parents=True)
    content = "export function foo() {}\n"
    full.write_text(content)

    assert real_fleet._no_effective_changes(
        str(workspace), [rel], {rel: content}
    ) is True


def test_no_effective_changes_false_si_hay_diferencia(real_fleet, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rel = "src/foo.ts"
    full = workspace / rel
    full.parent.mkdir(parents=True)
    full.write_text("export function foo() { return 1; }\n")

    assert real_fleet._no_effective_changes(
        str(workspace), [rel], {rel: "export function foo() {}\n"}
    ) is False


def test_no_effective_changes_false_si_archivo_nuevo(real_fleet, tmp_path):
    """Un archivo que no estaba en existing_files (nuevo del ticket) siempre
    cuenta como cambio real, aunque sea el único archivo aplicado."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rel = "src/nuevo.ts"
    full = workspace / rel
    full.parent.mkdir(parents=True)
    full.write_text("export function nuevo() {}\n")

    assert real_fleet._no_effective_changes(str(workspace), [rel], {}) is False


def test_validation_gate_rechaza_ciclo_sin_cambios_efectivos(real_fleet, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rel = "src/foo.ts"
    full = workspace / rel
    full.parent.mkdir(parents=True)
    content = "export function foo() {}\n"
    full.write_text(content)

    state = {
        "workspace_path": str(workspace),
        "applied_files": [rel],
        "existing_files": {rel: content},
        "stack": "node",
        "regression_errors": [],
    }

    result = real_fleet.validation_gate_node(state)

    assert result["validation_passed"] is False
    assert "Sin cambios producidos" in result["validation_report"]


def test_validation_gate_no_bloquea_ciclo_con_cambio_real(real_fleet, tmp_path, monkeypatch):
    """Con un cambio real, el gate debe seguir su curso normal (no debe
    quedar atrapado por la nueva guarda) — se stubea `_validate_workspace`
    para no depender de un proyecto Node real."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rel = "src/foo.ts"
    full = workspace / rel
    full.parent.mkdir(parents=True)
    full.write_text("export function foo() { return 2; }\n")

    monkeypatch.setattr(
        real_fleet, "_validate_workspace", lambda *a, **k: (True, "ok")
    )

    state = {
        "workspace_path": str(workspace),
        "applied_files": [rel],
        "existing_files": {rel: "export function foo() {}\n"},
        "stack": "node",
        "regression_errors": [],
    }

    result = real_fleet.validation_gate_node(state)

    assert result["validation_passed"] is True
