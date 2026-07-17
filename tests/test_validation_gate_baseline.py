"""
Regresión para el requerimiento "validation_gate/quality_reviewer deben
aprobar según el alcance del ticket, no exigir que TODA la suite del repo
esté verde" (ver
requerimientos/10-validation-gate-exige-suite-completa-no-alcance-del-ticket.md).

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
    spec = importlib.util.spec_from_file_location("langgraph_fleet_real_validation_gate", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_validation_gate_baseline")


VITEST_OUTPUT_BASELINE = """
 RUN  v1.0.0

 FAIL  src/server/auth/clerk-webhook-service.test.ts > ClerkWebhookService > user.deleted > returns ignored when user not found
AssertionError: expected 'User not found' to equal 'User not found in database'

 Test Files  1 failed | 3 passed (4)
      Tests  1 failed | 12 passed (13)
"""

VITEST_OUTPUT_POST_SIN_NUEVOS_FALLOS = """
 RUN  v1.0.0

 FAIL  src/server/auth/clerk-webhook-service.test.ts > ClerkWebhookService > user.deleted > returns ignored when user not found
AssertionError: expected 'User not found' to equal 'User not found in database'

 Test Files  1 failed | 4 passed (5)
      Tests  1 failed | 13 passed (14)
"""

VITEST_OUTPUT_POST_CON_FALLO_NUEVO = """
 RUN  v1.0.0

 FAIL  src/server/auth/clerk-webhook-service.test.ts > ClerkWebhookService > user.deleted > returns ignored when user not found
AssertionError: expected 'User not found' to equal 'User not found in database'

 FAIL  src/server/payments/checkout-webhook-service.test.ts > afterEach cleanup > resets mocks
ReferenceError: afterEach is not defined

 Test Files  2 failed | 3 passed (5)
      Tests  2 failed | 12 passed (14)
"""


def test_extract_failing_tests_parsea_lineas_fail(real_fleet):
    failing = real_fleet._extract_failing_tests(VITEST_OUTPUT_BASELINE)
    assert len(failing) == 1
    assert any("clerk-webhook-service.test.ts" in f for f in failing)


def _make_fake_run(vitest_output: str, rc: int = 1):
    def fake_run(cmd, cwd, timeout=120):
        if cmd[:1] == ["npx"]:
            return 0, "npx/10.0.0"
        if any("vitest" in part for part in cmd):
            return rc, vitest_output
        return 0, ""
    return fake_run


def test_ticket_acotado_se_aprueba_aunque_fallo_preexistente_siga_roto(real_fleet, monkeypatch, tmp_path):
    """Criterio de aceptación 1 y 3: un ticket que arregla exactamente lo que
    pide debe aprobarse aunque un test preexistente no relacionado siga roto,
    siempre que ya estuviera roto en el baseline."""
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")

    monkeypatch.setattr(real_fleet, "_run", _make_fake_run(VITEST_OUTPUT_BASELINE))
    baseline = real_fleet._run_vitest_baseline(str(tmp_path), "node")
    assert baseline is not None
    assert len(baseline) == 1

    monkeypatch.setattr(real_fleet, "_run", _make_fake_run(VITEST_OUTPUT_POST_SIN_NUEVOS_FALLOS))
    passed, report = real_fleet._validate_workspace(
        str(tmp_path), ["src/server/payments/checkout-webhook-service.test.ts"], "node",
        baseline_failing_tests=baseline,
    )

    assert passed is True
    assert "sin fallos nuevos" in report.lower()


def test_regresion_nueva_introducida_por_el_ticket_se_rechaza(real_fleet, monkeypatch, tmp_path):
    """Criterio de aceptación 2: un fallo NUEVO (no estaba en el baseline) sí
    debe bloquear la aprobación."""
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")

    monkeypatch.setattr(real_fleet, "_run", _make_fake_run(VITEST_OUTPUT_BASELINE))
    baseline = real_fleet._run_vitest_baseline(str(tmp_path), "node")

    monkeypatch.setattr(real_fleet, "_run", _make_fake_run(VITEST_OUTPUT_POST_CON_FALLO_NUEVO))
    passed, report = real_fleet._validate_workspace(
        str(tmp_path), ["src/server/payments/checkout-webhook-service.test.ts"], "node",
        baseline_failing_tests=baseline,
    )

    assert passed is False
    assert "nuevo" in report.lower()
    assert "checkout-webhook-service.test.ts" in report
    # el fallo preexistente (clerk-webhook-service) no debe listarse como "nuevo"
    assert "clerk-webhook-service" not in report.split("NUEVO")[-1] if "NUEVO" in report else True


def test_sin_baseline_disponible_cae_a_comportamiento_estricto_anterior(real_fleet, monkeypatch, tmp_path):
    """Si no se pudo capturar baseline (ej. proyecto sin vitest.config detectable
    en ese momento), cualquier fallo sigue bloqueando — comportamiento seguro
    por defecto, sin asumir qué era preexistente."""
    (tmp_path / "vitest.config.ts").write_text("export default {}\n")
    monkeypatch.setattr(real_fleet, "_run", _make_fake_run(VITEST_OUTPUT_POST_SIN_NUEVOS_FALLOS))

    passed, report = real_fleet._validate_workspace(
        str(tmp_path), ["src/server/payments/checkout-webhook-service.test.ts"], "node",
        baseline_failing_tests=None,
    )

    assert passed is False
