"""
Regresión para el requerimiento "el agente Node reescribe archivos de
implementación aunque el requerimiento pida explícitamente tocar solo tests"
(ver requerimientos/11-agente-modifica-implementacion-cuando-solo-se-pidio-tocar-tests.md).

Mismo patrón que los otros tests de langgraph_fleet: carga el módulo real por
ruta de archivo, con sus dependencias externas pesadas stubbeadas.
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
    spec = importlib.util.spec_from_file_location("langgraph_fleet_real_scope_guard", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_scope_protected_impl_files")


def _file_block(rel_path: str, content: str) -> str:
    return f"===FILE_BEGIN: {rel_path}===\n{content}===FILE_END==="


# ---------------------------------------------------------------------------
# _find_implicitly_protected_impl_files
# ---------------------------------------------------------------------------

def test_protege_implementacion_no_mencionada_del_todo(real_fleet):
    """Caso motivador (punto 5 del bug): el requerimiento solo menciona
    route.test.ts, nunca route.ts — debe protegerse en modo 'hard'."""
    criteria = (
        "Punto 5: corrige el test src/app/api/stripe/webhooks/route.test.ts "
        "para que refleje el nuevo comportamiento esperado."
    )
    protected = real_fleet._find_implicitly_protected_impl_files(criteria)
    assert protected.get("src/app/api/stripe/webhooks/route.ts") == "hard"


def test_no_protege_si_la_implementacion_tambien_se_menciona(real_fleet):
    criteria = (
        "Ajusta src/lib/foo.test.ts y también src/lib/foo.ts si hace falta "
        "para que el comportamiento sea consistente."
    )
    protected = real_fleet._find_implicitly_protected_impl_files(criteria)
    assert "src/lib/foo.ts" not in protected


def test_protege_en_modo_soft_si_hay_lenguaje_de_excepcion_condicional(real_fleet):
    """Caso motivador (punto 2 del bug): 'ajusta el test, no la implementación,
    salvo que confirmes que la implementación tiene el bug real' — modo 'soft',
    no bloquea, pero se marca."""
    criteria = (
        "Ajustar el TEST src/server/auth/clerk-webhook-service.test.ts "
        "(no la implementacion, salvo que confirmes que la implementacion "
        "tiene el bug real)."
    )
    protected = real_fleet._find_implicitly_protected_impl_files(criteria)
    assert protected.get("src/server/auth/clerk-webhook-service.ts") == "soft"


# ---------------------------------------------------------------------------
# _apply_workspace_changes con criteria
# ---------------------------------------------------------------------------

def test_rechaza_implementacion_no_autorizada_cuando_solo_se_pidio_el_test(real_fleet, tmp_path):
    criteria = (
        "Corrige el test src/app/api/stripe/webhooks/route.test.ts para "
        "verificar el nuevo comportamiento del webhook."
    )
    impl_dir = tmp_path / "src" / "app" / "api" / "stripe" / "webhooks"
    impl_dir.mkdir(parents=True)
    original_impl = "export async function POST() { return new Response('old'); }\n"
    (impl_dir / "route.ts").write_text(original_impl)

    llm_response = _file_block(
        "src/app/api/stripe/webhooks/route.ts",
        "export async function POST() { throw new Error('changed without permission'); }\n",
    )

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == []
    assert len(rejected) == 1
    assert "route.ts" in rejected[0]
    assert (impl_dir / "route.ts").read_text() == original_impl


def test_permite_implementacion_con_excepcion_condicional_pero_no_la_rechaza(real_fleet, tmp_path):
    """Modo 'soft': no se bloquea automáticamente (a diferencia del modo
    'hard'), aunque quede registrado para el reviewer."""
    criteria = (
        "Ajustar el TEST src/server/auth/clerk-webhook-service.test.ts "
        "(no la implementacion, salvo que confirmes que la implementacion "
        "tiene el bug real)."
    )
    impl_dir = tmp_path / "src" / "server" / "auth"
    impl_dir.mkdir(parents=True)
    (impl_dir / "clerk-webhook-service.ts").write_text("export class ClerkWebhookService {}\n")

    llm_response = _file_block(
        "src/server/auth/clerk-webhook-service.ts",
        "export class ClerkWebhookService { handle() { return true; } }\n",
    )

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response, criteria=criteria)

    assert applied == ["src/server/auth/clerk-webhook-service.ts"]
    assert rejected == []


def test_sin_criteria_no_activa_ninguna_proteccion(real_fleet, tmp_path):
    """Caso negativo: si no se pasa `criteria` (o está vacío), el comportamiento
    es el mismo que antes de este requerimiento — no debe romper nada."""
    impl_dir = tmp_path / "src"
    impl_dir.mkdir()
    (impl_dir / "route.ts").write_text("old\n")

    llm_response = _file_block("src/route.ts", "new\n")

    applied, rejected = real_fleet._apply_workspace_changes(str(tmp_path), llm_response)

    assert applied == ["src/route.ts"]
    assert rejected == []
