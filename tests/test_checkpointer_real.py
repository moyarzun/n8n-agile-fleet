"""
Spec langgraph-hardening — Req 1 (checkpointer durable) y Req 5.2/5.3
(@task por agent_role).

Todos estos tests requieren el langgraph REAL (checkpointer, functional API):
se saltan en el entorno local y corren dentro del contenedor de la flota
(tarea 8.1 de la spec).
"""
import pytest

pytest.importorskip("langgraph.checkpoint.sqlite",
                    reason="requiere langgraph real (corre dentro del contenedor)")

from tests._real_fleet_loader import load_real_langgraph_fleet


@pytest.fixture(scope="module")
def real_fleet():
    return load_real_langgraph_fleet("langgraph_fleet_real_checkpointer")


@pytest.fixture()
def fresh_checkpointer(real_fleet, tmp_path, monkeypatch):
    """Redirige FLEET_CHECKPOINT_DB a un archivo temporal y resetea el
    singleton para que cada test tenga su propio archivo de checkpoints."""
    db_path = tmp_path / "checkpoints.db"
    monkeypatch.setenv("FLEET_CHECKPOINT_DB", str(db_path))
    real_fleet._checkpointer = None
    yield db_path
    real_fleet._checkpointer = None


def test_get_checkpointer_crea_el_archivo_si_no_existe(real_fleet, fresh_checkpointer):
    """Req 1.4: el archivo se crea sin intervención manual."""
    assert not fresh_checkpointer.exists()
    saver = real_fleet._get_checkpointer()
    assert saver is not None
    assert real_fleet._get_checkpointer() is saver  # singleton


def test_estado_persiste_por_thread_y_delete_lo_elimina(real_fleet, fresh_checkpointer):
    """Req 1.1/1.3: el estado se checkpointea bajo el thread_id del job y
    delete_job_checkpoints lo elimina."""
    from langgraph.graph import StateGraph, START, END
    from typing_extensions import TypedDict

    class S(TypedDict):
        x: int

    def inc(state):
        return {"x": state["x"] + 1}

    g = StateGraph(S)
    g.add_node("inc", inc)
    g.add_edge(START, "inc")
    g.add_edge("inc", END)
    compiled = g.compile(checkpointer=real_fleet._get_checkpointer())

    cfg = real_fleet.invoke_config("job-777")
    out = compiled.invoke({"x": 1}, cfg)
    assert out["x"] == 2

    saver = real_fleet._get_checkpointer()
    assert saver.get_tuple(cfg) is not None       # checkpoint persistido
    real_fleet.delete_job_checkpoints("job-777")   # Req 1.3
    assert saver.get_tuple(cfg) is None


def test_task_por_agent_role_no_se_reinvoca_al_reanudar(real_fleet, fresh_checkpointer):
    """Req 5.2/5.3: la invocación LLM de cada agent_role es un @task
    checkpointeable — al reanudar tras un fallo a mitad de ciclo, los roles ya
    completados se sirven del checkpoint sin re-invocar el modelo."""
    from langgraph.graph import StateGraph, START, END
    from typing_extensions import TypedDict
    import types as _types

    calls = {"A": 0, "B": 0}
    fail_first_b = {"value": True}

    def fake_invoke_dev(messages):
        # messages[0] es SystemMessage cuyo content encodea el rol (ver nodo)
        role = messages[0].content
        calls[role] += 1
        if role == "B" and fail_first_b["value"]:
            fail_first_b["value"] = False
            raise RuntimeError("crash simulado tras completar el agente A")
        return _types.SimpleNamespace(content=f"codigo de {role}")

    real_fleet._invoke_dev = fake_invoke_dev

    class S(TypedDict):
        results: list

    def dev_node(state):
        outs = []
        for role in ("A", "B"):
            gen = real_fleet._agent_generation(role, role, "instruccion")
            outs.append(gen.result() if hasattr(gen, "result") else gen)
        return {"results": outs}

    g = StateGraph(S)
    g.add_node("dev", dev_node)
    g.add_edge(START, "dev")
    g.add_edge("dev", END)
    compiled = g.compile(checkpointer=real_fleet._get_checkpointer())
    cfg = real_fleet.invoke_config("job-task-resume")

    with pytest.raises(RuntimeError):
        compiled.invoke({"results": []}, cfg)  # A completa, B crashea

    assert calls == {"A": 1, "B": 1}

    out = compiled.invoke(None, cfg)  # reanudación desde el checkpoint

    assert out["results"] == ["codigo de A", "codigo de B"]
    # A NO se re-invocó (su resultado vino del checkpoint del task); B sí.
    assert calls == {"A": 1, "B": 2}
