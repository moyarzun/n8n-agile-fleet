"""
Regresión para el requerimiento "_is_quota_error no reconoce el formato de
error de Nvidia" (ver requerimientos/05-quota-error-no-detecta-mensaje-de-nvidia.md).

Usa el mismo truco que tests/test_git_setup_node.py: carga el módulo real
agile_scripts/langgraph_fleet.py por ruta de archivo, con sus dependencias
externas pesadas (langgraph, langchain, openai, atlassian) stubbeadas, para
no depender de tenerlas instaladas y sin chocar con el mock de
sys.modules["langgraph_fleet"] que usa conftest.py para test_fleet_api.py.
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

    class _FakeRateLimitError(Exception):
        pass

    class _FakeAPIStatusError(Exception):
        def __init__(self, message="", status_code=None):
            super().__init__(message)
            self.status_code = status_code

    _stub_module("openai", RateLimitError=_FakeRateLimitError, APIStatusError=_FakeAPIStatusError)
    _stub_module("atlassian", Jira=object)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    module_path = os.path.join(repo_root, "agile_scripts", "langgraph_fleet.py")
    spec = importlib.util.spec_from_file_location("langgraph_fleet_real_quota", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def real_fleet():
    from tests._real_fleet_loader import load_real_langgraph_fleet
    return load_real_langgraph_fleet("langgraph_fleet_real_quota_error_detection")


NVIDIA_QUOTA_MESSAGE = (
    "Upstream error from Nvidia: ResourceExhausted: Worker local total "
    "request limit reached (34/32)"
)


def test_detecta_mensaje_real_de_nvidia_como_error_de_cuota(real_fleet):
    exc = RuntimeError(NVIDIA_QUOTA_MESSAGE)
    assert real_fleet._is_quota_error(exc) is True


CONTEXT_LENGTH_MESSAGE = (
    "Error code: 400 - {'message': \"This endpoint's maximum context length is "
    "65536 tokens. However, you requested about 76822 tokens (35862 of text "
    "input, 40960 in the output).\"}"
)


def test_detecta_error_de_limite_de_contexto_como_error_de_cuota(real_fleet):
    """Regresión requerimiento 09: un modelo con ventana chica debe tratarse
    como fallback-able (avanzar al siguiente modelo), no como fallo fatal."""
    exc = RuntimeError(CONTEXT_LENGTH_MESSAGE)
    assert real_fleet._is_quota_error(exc) is True


def test_invoke_chain_avanza_al_siguiente_modelo_con_error_de_nvidia(real_fleet):
    class FakeModel:
        def __init__(self, name, raises=None):
            self.model_name = name
            self._raises = raises

        def invoke(self, messages):
            if self._raises is not None:
                raise self._raises
            return types.SimpleNamespace(content="ok", response_metadata={})

    primary = FakeModel("nvidia-saturado", raises=RuntimeError(NVIDIA_QUOTA_MESSAGE))
    fallback = FakeModel("siguiente-modelo")

    response = real_fleet._invoke_chain(primary, [fallback], ["hola"])

    assert response.content == "ok"


def test_invoke_chain_no_atrapa_errores_que_no_son_de_cuota(real_fleet):
    class FakeModel:
        def __init__(self, name, raises=None):
            self.model_name = name
            self._raises = raises

        def invoke(self, messages):
            if self._raises is not None:
                raise self._raises
            return types.SimpleNamespace(content="ok", response_metadata={})

    boom = ValueError("algo totalmente distinto, un error de validacion de esquema")
    primary = FakeModel("modelo-roto", raises=boom)
    fallback = FakeModel("nunca-llamado")

    with pytest.raises(ValueError):
        real_fleet._invoke_chain(primary, [fallback], ["hola"])


AKAMAI_ERROR_PAGE = (
    "<HTML><HEAD><TITLE>Error</TITLE></HEAD><BODY>\n"
    "An error occurred while processing your request.<p>\n"
    "Reference #221.245f1cc8.1752678123.abcdef\n"
    "<P>https://errors.edgesuite.net/221.245f1cc8.1752678123.abcdef</P>\n"
    "</BODY></HTML>"
)


def test_detecta_pagina_de_error_de_gateway_tipo_akamai(real_fleet):
    assert real_fleet._looks_like_gateway_error_page(AKAMAI_ERROR_PAGE) is True
    assert real_fleet._looks_like_gateway_error_page("contenido normal del modelo") is False
    assert real_fleet._looks_like_gateway_error_page("") is False


def test_invoke_chain_reintenta_tras_pagina_de_error_de_gateway_y_luego_funciona(real_fleet, monkeypatch):
    """Regresión requerimiento 07 (Akamai): una respuesta HTML de error del
    gateway del proveedor debe disparar reintento automático con backoff
    corto; si el reintento tiene éxito, el ciclo continúa normalmente."""
    monkeypatch.setattr(real_fleet.time, "sleep", lambda seconds: None)

    calls = []

    class FlakyGatewayModel:
        model_name = "modelo-con-gateway-caido"

        def invoke(self, messages):
            calls.append(1)
            if len(calls) == 1:
                return types.SimpleNamespace(content=AKAMAI_ERROR_PAGE, response_metadata={})
            return types.SimpleNamespace(content="===FILE_BEGIN: a.py===\nok\n===FILE_END===", response_metadata={})

    response = real_fleet._invoke_chain(FlakyGatewayModel(), [], ["hola"])

    assert len(calls) == 2
    assert "FILE_BEGIN" in response.content


def test_invoke_chain_agota_reintentos_de_gateway_y_reporta_error_de_proveedor(real_fleet, monkeypatch):
    """Si el gateway sigue caído tras agotar los reintentos, debe fallar con
    un mensaje que identifique claramente que es un error del proveedor
    (distinto de un rechazo por calidad de código)."""
    monkeypatch.setattr(real_fleet.time, "sleep", lambda seconds: None)

    class AlwaysGatewayErrorModel:
        model_name = "modelo-siempre-caido"

        def invoke(self, messages):
            return types.SimpleNamespace(content=AKAMAI_ERROR_PAGE, response_metadata={})

    with pytest.raises(RuntimeError, match="[Ee]rror del proveedor"):
        real_fleet._invoke_chain(AlwaysGatewayErrorModel(), [], ["hola"])
