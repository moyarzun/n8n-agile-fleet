"""
Spec de campo — requerimiento 14: el planner devolvía 0 subtareas de forma
silenciosa. Ahora reintenta una vez y, si persiste, deja un aviso explícito y
continúa con el fallback (criteria completo) — sin crashear.
"""
import types

import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_planner")


def _state():
    return {
        "acceptance_criteria": "TITULO: fix chico\n\nCRITERIOS:\nAgrega un helper trivial.",
        "stack": "node",
        "workspace_path": "/tmp/no-usado",
    }


def test_planner_reintenta_una_vez_ante_cero_subtareas(real_fleet, monkeypatch):
    """El primer intento devuelve array vacío, el segundo devuelve subtareas
    reales — el planner debe reintentar y quedarse con el segundo."""
    calls = []

    def fake_invoke_reviewer(messages):
        calls.append(1)
        content = "[]" if len(calls) == 1 else '["Crear src/lib/x.ts", "Agregar src/lib/x.test.ts"]'
        return types.SimpleNamespace(content=content)

    monkeypatch.setattr(real_fleet, "_invoke_reviewer", fake_invoke_reviewer)

    result = real_fleet.planner_node(_state())

    assert len(calls) == 2  # reintentó
    assert len(result["subtasks"]) == 2


def test_planner_cero_subtareas_persistente_no_crashea_y_avisa(real_fleet, monkeypatch):
    """Ambos intentos devuelven vacío — el planner NO debe crashear; procede con
    fallback (subtasks vacías) dejando el aviso explícito en el log."""
    logs = []
    monkeypatch.setattr(real_fleet, "_log", lambda msg: logs.append(msg))
    monkeypatch.setattr(real_fleet, "_invoke_reviewer",
                        lambda messages: types.SimpleNamespace(content="[]"))

    result = real_fleet.planner_node(_state())

    assert result["subtasks"] == []
    assert any("AVISO" in l and "fallback" in l.lower() for l in logs)


def test_planner_tolera_excepcion_del_modelo_sin_crashear(real_fleet, monkeypatch):
    """Si el modelo revisor lanza, el planner no debe propagar la excepción —
    cae al fallback tras reintentar."""
    def boom(messages):
        raise RuntimeError("modelo caído")

    monkeypatch.setattr(real_fleet, "_invoke_reviewer", boom)

    result = real_fleet.planner_node(_state())  # no debe lanzar
    assert result["subtasks"] == []
