"""
Regresión para el requerimiento "dynamic_developer alucina resultado de
comandos npm/npx, no los ejecuta — reescribe package.json con un árbol de
dependencias inventado" (ver
requerimientos/18-dynamic-developer-alucina-resultado-de-comandos-npm-no-los-ejecuta.md).

Caso motivador real: TASK-2cd6964f pidió agregar 2 devDependencies de testing
a mobile/package.json y el LLM devolvió, además, downgrades de 2 versiones
mayores en dependencias centrales (react, expo-router, react-native) y la
eliminación de 7 paquetes usados activamente — nada de eso estaba en el
requerimiento.
"""
import json

import pytest


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_package_json_guard")


def _file_block(rel_path: str, content: str) -> str:
    return f"===FILE_BEGIN: {rel_path}===\n{content}===FILE_END==="


ORIGINAL_PACKAGE_JSON = {
    "name": "tennis-coach-mobile",
    "version": "1.0.0",
    "dependencies": {
        "@clerk/clerk-expo": "^2.5.0",
        "expo-router": "~6.0.23",
        "react": "^19.1.0",
        "react-native": "^0.81.5",
        "react-native-reanimated": "~4.1.1",
    },
    "devDependencies": {
        "typescript": "~5.7.0",
    },
}


# ---------------------------------------------------------------------------
# _find_unauthorized_package_json_dependency_changes
# ---------------------------------------------------------------------------

def test_agregar_devdependency_mencionada_no_genera_violaciones(real_fleet):
    """Criterio de aceptación 1/2: el caso normal — pedir agregar 1-2
    devDependencies nuevas no debe generar ninguna violación."""
    new_pkg = json.loads(json.dumps(ORIGINAL_PACKAGE_JSON))
    new_pkg["devDependencies"]["jest-expo"] = "~54.0.0"
    new_pkg["devDependencies"]["@testing-library/react-native"] = "^12.4.0"

    criteria = (
        "Desde mobile/, agregar jest-expo y @testing-library/react-native "
        "como devDependencies."
    )
    violations = real_fleet._find_unauthorized_package_json_dependency_changes(
        json.dumps(ORIGINAL_PACKAGE_JSON), json.dumps(new_pkg), criteria,
    )
    assert violations == []


def test_downgrade_de_dependencia_no_mencionada_es_violacion(real_fleet):
    """Caso motivador: react downgradeado de 19 a 18 sin que el requerimiento
    lo mencione — debe detectarse."""
    new_pkg = json.loads(json.dumps(ORIGINAL_PACKAGE_JSON))
    new_pkg["dependencies"]["react"] = "18.3.1"

    criteria = "Desde mobile/, agregar jest-expo como devDependency."
    violations = real_fleet._find_unauthorized_package_json_dependency_changes(
        json.dumps(ORIGINAL_PACKAGE_JSON), json.dumps(new_pkg), criteria,
    )
    assert any("react" in v and "18.3.1" in v for v in violations)


def test_eliminacion_de_dependencia_no_mencionada_es_violacion(real_fleet):
    """Caso motivador: react-native-reanimated eliminado sin que el
    requerimiento lo mencione — debe detectarse."""
    new_pkg = json.loads(json.dumps(ORIGINAL_PACKAGE_JSON))
    del new_pkg["dependencies"]["react-native-reanimated"]

    criteria = "Desde mobile/, agregar jest-expo como devDependency."
    violations = real_fleet._find_unauthorized_package_json_dependency_changes(
        json.dumps(ORIGINAL_PACKAGE_JSON), json.dumps(new_pkg), criteria,
    )
    assert any("react-native-reanimated" in v for v in violations)


def test_cambio_de_dependencia_mencionada_por_nombre_esta_autorizado(real_fleet):
    """Si el requerimiento sí menciona el paquete por nombre (ej. pide
    actualizar esa versión específica), el cambio está autorizado."""
    new_pkg = json.loads(json.dumps(ORIGINAL_PACKAGE_JSON))
    new_pkg["dependencies"]["expo-router"] = "~6.1.0"

    criteria = "Desde mobile/, actualizar expo-router a la última versión ~6.1.0."
    violations = real_fleet._find_unauthorized_package_json_dependency_changes(
        json.dumps(ORIGINAL_PACKAGE_JSON), json.dumps(new_pkg), criteria,
    )
    assert violations == []


def test_json_invalido_no_opina(real_fleet):
    """No debe reventar ni bloquear si alguno de los dos contenidos no es
    JSON válido — eso lo maneja otra parte del pipeline."""
    violations = real_fleet._find_unauthorized_package_json_dependency_changes(
        "{not valid json", json.dumps(ORIGINAL_PACKAGE_JSON), "cualquier cosa",
    )
    assert violations == []


# ---------------------------------------------------------------------------
# _apply_workspace_changes con package.json — criterio de aceptación 2
# ---------------------------------------------------------------------------

def test_caso_exacto_del_hallazgo_downgrades_y_eliminaciones_masivas_rechazado(real_fleet, tmp_path):
    """Reproduce TASK-2cd6964f: requerimiento pide agregar 2 devDependencies
    de testing; el LLM devuelve además downgrades masivos y eliminaciones no
    pedidas — el archivo completo se rechaza (hard) y no se escribe."""
    mobile = tmp_path / "mobile"
    mobile.mkdir()
    (mobile / "package.json").write_text(json.dumps(ORIGINAL_PACKAGE_JSON, indent=2) + "\n")

    hallucinated = {
        "name": "tennis-coach-pro-mobile",  # rename no pedido
        "version": "1.0.0",
        "dependencies": {
            "@clerk/clerk-expo": "^0.23.7",  # downgrade no pedido
            "expo-router": "~4.0.19",  # downgrade no pedido
            "react": "18.3.1",  # downgrade no pedido
            "react-native": "0.76.7",  # downgrade no pedido
            # react-native-reanimated eliminado sin pedirlo
        },
        "devDependencies": {
            "typescript": "~5.3.3",  # downgrade no pedido
            "jest-expo": "~54.0.0",  # esto sí se pidió
            "@testing-library/react-native": "^12.4.0",  # esto sí se pidió
        },
    }

    criteria = (
        "Desde el directorio mobile/, correr `npx expo install jest-expo --check`. "
        "Luego correr `npm install --save-dev @testing-library/react-native "
        "react-test-renderer` desde mobile/."
    )
    llm_response = _file_block("mobile/package.json", json.dumps(hallucinated, indent=2) + "\n")

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == []
    assert len(rejected) == 1
    assert "package.json" in rejected[0]
    # El archivo en disco queda intacto — ninguna dependencia real se toca
    on_disk = json.loads((mobile / "package.json").read_text())
    assert on_disk == ORIGINAL_PACKAGE_JSON


def test_agregar_devdependencies_pedidas_sin_tocar_el_resto_se_aplica(real_fleet, tmp_path):
    """Caso positivo: el LLM se porta bien — agrega solo lo pedido, preserva
    el resto del árbol intacto — se aplica con normalidad."""
    mobile = tmp_path / "mobile"
    mobile.mkdir()
    (mobile / "package.json").write_text(json.dumps(ORIGINAL_PACKAGE_JSON, indent=2) + "\n")

    well_behaved = json.loads(json.dumps(ORIGINAL_PACKAGE_JSON))
    well_behaved["devDependencies"]["jest-expo"] = "~54.0.0"
    well_behaved["devDependencies"]["@testing-library/react-native"] = "^12.4.0"

    criteria = (
        "Desde mobile/, agregar jest-expo y @testing-library/react-native "
        "como devDependencies."
    )
    llm_response = _file_block("mobile/package.json", json.dumps(well_behaved, indent=2) + "\n")

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == ["mobile/package.json"]
    assert rejected == []


def test_package_json_nuevo_no_activa_la_guarda(real_fleet, tmp_path):
    """Caso negativo: un package.json que no existe todavía en disco (proyecto
    nuevo) no tiene 'original' contra el cual comparar — no debe bloquearse."""
    criteria = "Crea un package.json inicial para el paquete."
    llm_response = _file_block(
        "nuevo-paquete/package.json",
        json.dumps({"name": "nuevo-paquete", "version": "0.1.0"}, indent=2) + "\n",
    )

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == ["nuevo-paquete/package.json"]
    assert rejected == []
