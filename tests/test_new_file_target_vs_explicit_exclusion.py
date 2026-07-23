"""
Requerimiento 22: la guarda de exclusión explícita (`_find_explicitly_forbidden_files`)
capturaba el archivo NUEVO que el ticket pedía crear como si fuera uno de los
archivos prohibidos, cuando el mismo párrafo de alcance mencionaba también
archivos existentes a NO tocar ("SOLO crear el archivo nuevo X. No modificar
Y, Z."). Caso real: TASK-e283fd8f, `.github/workflows/mobile-tests.yml`
rechazado en los 6 ciclos porque era exactamente el archivo que había que
crear.
"""
import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_new_file_target")


def test_archivo_nuevo_declarado_no_termina_prohibido(real_fleet):
    criteria = (
        "Alcance: SOLO crear el archivo nuevo `.github/workflows/mobile-tests.yml`. "
        "No modificar `quick-checks.yml`, `test-battery.yml`, ni ningún otro workflow "
        "existente."
    )
    forbidden = real_fleet._find_explicitly_forbidden_files(criteria)

    assert ".github/workflows/mobile-tests.yml" not in forbidden
    assert "quick-checks.yml" in forbidden


def test_archivos_existentes_mencionados_siguen_prohibidos(real_fleet, tmp_path):
    """El fix no debe desactivar la protección real sobre los archivos que sí
    se pidió no tocar — solo excluir al archivo nuevo declarado."""
    criteria = (
        "Alcance: SOLO crear el archivo nuevo `.github/workflows/mobile-tests.yml`. "
        "No modificar `quick-checks.yml`."
    )
    llm_response = (
        "===FILE_BEGIN: .github/workflows/mobile-tests.yml===\n"
        "name: mobile tests\n"
        "===FILE_END===\n"
        "===FILE_BEGIN: quick-checks.yml===\n"
        "name: quick checks modificado\n"
        "===FILE_END===\n"
    )
    workspace = tmp_path / "repo"
    workflows = workspace / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "quick-checks.yml").write_text("name: quick checks\n")

    applied, rejected = real_fleet._apply_workspace_changes(
        str(workspace), llm_response, criteria=criteria
    )

    assert ".github/workflows/mobile-tests.yml" in applied
    assert not any("mobile-tests.yml" in r for r in rejected)
    assert any("quick-checks.yml" in r for r in rejected)


def test_sin_marcador_de_archivo_nuevo_comportamiento_previo_intacto(real_fleet):
    """Sin la frase "crear el archivo nuevo", el comportamiento de la guarda
    de exclusión explícita (requerimiento 15/17) no cambia."""
    criteria = "No modifiques `src/server/auth/context.ts` en este ticket."
    forbidden = real_fleet._find_explicitly_forbidden_files(criteria)
    assert forbidden == {"src/server/auth/context.ts"}
