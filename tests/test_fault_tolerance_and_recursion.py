"""
Spec langgraph-hardening — Req 4 (tolerancia a fallos por nodo) y Req 7
(límite de recursión explícito).

Los tests de lógica pura (handler, router, config) corren en cualquier
entorno; los que ejercitan RetryPolicy real de LangGraph usan importorskip y
corren dentro del contenedor.
"""
import pytest

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_fault_tolerance")


# ---------------------------------------------------------------------------
# Req 1.2 / 7.1 — invoke_config
# ---------------------------------------------------------------------------

def test_invoke_config_incluye_thread_id_y_recursion_limit(real_fleet):
    cfg = real_fleet.invoke_config("job-abc")
    assert cfg["configurable"]["thread_id"] == "job-abc"
    assert cfg["recursion_limit"] == 60


# ---------------------------------------------------------------------------
# Req 4.3 — error handler global
# ---------------------------------------------------------------------------

class _FakeNodeErrorInfo:
    node = "validation_gate"
    error = RuntimeError("disco lleno")


def test_error_handler_escribe_aborted_y_feedback_con_nodo_y_mensaje(real_fleet):
    result = real_fleet._node_error_handler({}, _FakeNodeErrorInfo())

    assert result["aborted"] is True
    assert result["is_approved"] is False
    assert result["validation_passed"] is False
    assert "validation_gate" in result["reviewer_feedback"]
    assert "disco lleno" in result["reviewer_feedback"]


def test_reviewer_hace_fast_reject_si_el_ciclo_esta_abortado(real_fleet):
    state = {
        "workspace_path": "/tmp/no-usado",
        "acceptance_criteria": "x",
        "applied_files": ["a.py"],
        "aborted": True,
        "reviewer_feedback": "ABORTADO: el nodo planner falló",
    }
    result = real_fleet.reviewer_node(state)
    assert result["is_approved"] is False
    assert "planner" in result["reviewer_feedback"]


# ---------------------------------------------------------------------------
# Req 4.3 / 7.4 — quality_gate_router
# ---------------------------------------------------------------------------

def _router_state(**overrides):
    state = {
        "is_approved": False,
        "validation_passed": False,
        "loop_iterations": 1,
        "remaining_steps": 50,
    }
    state.update(overrides)
    return state


def test_router_desvia_a_git_finalize_si_aborted(real_fleet):
    assert real_fleet.quality_gate_router(_router_state(aborted=True)) == "git_finalize"


def test_router_corta_ordenadamente_con_remaining_steps_bajo(real_fleet):
    assert real_fleet.quality_gate_router(_router_state(remaining_steps=7)) == "git_finalize"


def test_router_sigue_iterando_con_remaining_steps_suficientes(real_fleet):
    assert real_fleet.quality_gate_router(_router_state(remaining_steps=20)) == "dynamic_developer"


def test_router_comportamiento_previo_intacto(real_fleet):
    # aprobado+validado → cierre
    assert real_fleet.quality_gate_router(
        _router_state(is_approved=True, validation_passed=True)) == "git_finalize"
    # límite de ciclos → cierre
    assert real_fleet.quality_gate_router(_router_state(loop_iterations=6)) == "git_finalize"


# ---------------------------------------------------------------------------
# Req 4.1 / 4.5 / 4.6 — RetryPolicy real (solo con langgraph instalado)
# ---------------------------------------------------------------------------

def test_retry_policy_reintenta_fallo_transitorio_pero_no_valueerror():
    pytest.importorskip("langgraph.checkpoint.memory",
                        reason="requiere langgraph real (corre dentro del contenedor)")
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import RetryPolicy
    from typing_extensions import TypedDict
    import httpx

    class S(TypedDict):
        x: int

    transient_calls = []

    def transient_node(state):
        transient_calls.append(1)
        if len(transient_calls) == 1:
            raise httpx.ConnectError("transitorio")  # retry_on default lo reintenta
        return {"x": state["x"] + 1}

    g = StateGraph(S)
    g.add_node("n", transient_node, retry_policy=RetryPolicy(max_attempts=2))
    g.add_edge(START, "n")
    g.add_edge("n", END)
    compiled = g.compile(checkpointer=InMemorySaver())
    out = compiled.invoke({"x": 1}, {"configurable": {"thread_id": "t1"}})
    assert out["x"] == 2
    assert len(transient_calls) == 2  # falló una vez, reintentó y pasó

    # ValueError NO se reintenta (Req 4.5): un solo intento y la excepción sube
    value_calls = []

    def buggy_node(state):
        value_calls.append(1)
        raise ValueError("bug de programación")

    g2 = StateGraph(S)
    g2.add_node("n", buggy_node, retry_policy=RetryPolicy(max_attempts=3))
    g2.add_edge(START, "n")
    g2.add_edge("n", END)
    compiled2 = g2.compile(checkpointer=InMemorySaver())
    with pytest.raises(ValueError):
        compiled2.invoke({"x": 1}, {"configurable": {"thread_id": "t2"}})
    assert len(value_calls) == 1
