"""
Regresión para el requerimiento "el agente Node no respeta rutas/nombres de
archivo exactos cuando el requerimiento los especifica" (ver
requerimientos/08-agente-no-respeta-nombres-de-archivo-del-requerimiento.md).

Investigación punto 1: confirmar que codebase_reader_node efectivamente lee
un archivo de plan .md referenciado en el requerimiento y lo inyecta como
contexto — antes NO lo hacía porque la regex de extracción de rutas no
incluía la extensión .md, así que el LLM nunca veía el contenido real del
plan (solo el texto libre del requerimiento pidiéndole que "lo leyera").
"""
import importlib.util
import os
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
    _stub_module("langgraph.graph", StateGraph=object, START="START", END="END")
    _stub_module("langgraph.graph.message", add_messages=lambda *a, **k: None)

    class _FakeChatOpenAI:
        def __init__(self, *args, **kwargs):
            pass

    _stub_module("langchain_openai", ChatOpenAI=_FakeChatOpenAI)
    _stub_module("openai", RateLimitError=Exception, APIStatusError=Exception)
    _stub_module("atlassian", Jira=object)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    module_path = os.path.join(repo_root, "agile_scripts", "langgraph_fleet.py")
    spec = importlib.util.spec_from_file_location("langgraph_fleet_real_codebase_reader", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_codebase_reader_plan_files")


def test_codebase_reader_lee_archivo_de_plan_md_referenciado_en_el_requerimiento(real_fleet, tmp_path):
    plan_dir = tmp_path / "docs" / "superpowers" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-15-payments-webhooks-refactor.md"
    plan_content = (
        "# Plan: refactor de webhooks de pagos\n\n"
        "Crear: `src/server/payments/webhook-service.ts`\n"
        "Extender: `src/lib/clerk-webhook.ts`\n"
    )
    plan_path.write_text(plan_content)

    state = {
        "workspace_path": str(tmp_path),
        "stack": "node",
        "subtasks": [],
        "acceptance_criteria": (
            "TITULO: refactor webhooks\n\n"
            "CRITERIOS DE ACEPTACION:\n"
            "Lee el archivo completo primero — contiene el codigo exacto a escribir: "
            "docs/superpowers/plans/2026-07-15-payments-webhooks-refactor.md\n"
            "No te desvies de lo que el plan especifica."
        ),
    }

    result = real_fleet.codebase_reader_node(state)

    existing = result["existing_files"]
    plan_rel_path = "docs/superpowers/plans/2026-07-15-payments-webhooks-refactor.md"
    assert plan_rel_path in existing, (
        f"el plan .md referenciado en el requerimiento debe leerse; archivos capturados: {list(existing)}"
    )
    assert existing[plan_rel_path] == plan_content
