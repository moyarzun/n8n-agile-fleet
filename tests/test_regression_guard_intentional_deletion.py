"""
Requerimiento 21: la guarda de regresión de exports (`_check_ts_exports_regression`)
revertía la eliminación de una función/export cuando el ticket pedía
explícitamente borrarla, porque no distinguía "el ticket pidió eliminar X" de
"el generador perdió X por accidente". Caso real: TASK-d11c44eb, 6 ciclos
agotados intentando borrar `markAttendanceForClass` y su helper
`assertCanMarkAttendance` sin poder converger nunca.
"""
import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_intentional_deletion")


def test_eliminacion_pedida_por_nombre_no_es_regresion(real_fleet):
    criteria = (
        "Elimina la función `markAttendanceForClass` de "
        "src/server/classes/class-service.ts junto con su helper "
        "`assertCanMarkAttendance`, ya que ninguna quedó con callers."
    )
    old = (
        "export function markAttendanceForClass() {}\n"
        "export function assertCanMarkAttendance() {}\n"
        "export function otraFuncion() {}\n"
    )
    new = "export function otraFuncion() {}\n"  # ambas borradas, tal como se pidió

    issues = real_fleet._check_ts_exports_regression(old, new, criteria)

    assert issues == []


def test_eliminacion_no_pedida_ademas_de_la_pedida_sigue_rechazada(real_fleet):
    """Criterio 2: si además de lo pedido desaparece algo NO pedido, el guard
    debe seguir rechazando ese export."""
    criteria = "Elimina la función `markAttendanceForClass` (sin otros usos)."
    old = (
        "export function markAttendanceForClass() {}\n"
        "export function otraFuncionQueNoDebiaTocarse() {}\n"
    )
    new = ""  # se borró TODO, no solo lo pedido

    issues = real_fleet._check_ts_exports_regression(old, new, criteria)

    assert len(issues) == 1
    assert "otraFuncionQueNoDebiaTocarse" in issues[0]
    assert "markAttendanceForClass" not in issues[0]


def test_sin_criteria_comportamiento_previo_intacto(real_fleet):
    """Sin mención explícita de eliminación en el requerimiento, cualquier
    export perdido sigue reportándose como regresión (requerimiento 16)."""
    old = "export async function GET(req) { return a(); }\n"
    new = ""

    issues = real_fleet._check_ts_exports_regression(old, new)

    assert len(issues) == 1
    assert "GET" in issues[0]


def test_find_intentionally_removed_exports_solo_captura_backticks_cercanos(real_fleet):
    criteria = "Borra `foo` y `bar`. No toques nada más."
    names = real_fleet._find_intentionally_removed_exports(criteria)
    assert names == {"foo", "bar"}
