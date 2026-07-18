"""
Loader compartido del módulo REAL agile_scripts/langgraph_fleet.py para tests.

conftest.py reemplaza sys.modules["langgraph_fleet"] por un mock (para que
test_fleet_api.py importe fleet_api sin las dependencias pesadas). Los tests
que ejercitan la lógica real del grafo cargan el módulo por ruta de archivo
bajo un alias distinto.

Estrategia de dependencias "real si está disponible, stub si no":
- En el entorno local (sin langgraph/langchain instalados) se instalan stubs
  mínimos, suficientes para importar el módulo y ejercitar la lógica pura.
- Dentro del contenedor (deps reales instaladas) se usan los módulos reales —
  imprescindible para los tests marcados con importorskip (checkpointer real,
  @task, OTel), y NUNCA se muta un módulo real con atributos de stub.
- Excepción: langchain_openai SIEMPRE se stubbea — el módulo real construye
  clientes ChatOpenAI en el import de langgraph_fleet y validaría API keys.
"""
import importlib
import importlib.util
import os
import sys
import types


class _FakeChatOpenAI:
    def __init__(self, *args, **kwargs):
        pass


class _FakeRetryPolicy:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeGraphRecursionError(Exception):
    pass


class _FakeNodeError(Exception):
    pass


class _FakeRateLimitError(Exception):
    pass


class _FakeAPIStatusError(Exception):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _fake_task(fn=None, **kwargs):
    """Stub del decorador @task: identidad. El código de producción usa un
    guard hasattr(result, "result") así que devolver el valor directo (en vez
    de un future) funciona en ambos mundos."""
    if fn is not None:
        return fn
    return lambda f: f


def _stub_if_missing(name: str, **attrs) -> None:
    """Instala un stub SOLO si el módulo real no existe ni puede importarse.
    Nunca muta un módulo real ya importado."""
    existing = sys.modules.get(name)
    if existing is not None:
        if getattr(existing, "__fleet_test_stub__", False):
            for key, value in attrs.items():
                setattr(existing, key, value)
        return
    try:
        importlib.import_module(name)
        return  # el módulo real existe: usarlo tal cual
    except ImportError:
        pass
    mod = types.ModuleType(name)
    mod.__fleet_test_stub__ = True
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def _force_stub(name: str, **attrs) -> None:
    """Stub incondicional (reemplaza al real si estuviera importado)."""
    mod = types.ModuleType(name)
    mod.__fleet_test_stub__ = True
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def install_stubs() -> None:
    _stub_if_missing("langchain_core")
    _stub_if_missing(
        "langchain_core.messages",
        BaseMessage=object,
        HumanMessage=lambda *a, **k: None,
        SystemMessage=lambda *a, **k: None,
        AIMessage=lambda *a, **k: types.SimpleNamespace(content=k.get("content"), name=k.get("name")),
    )
    _stub_if_missing("langgraph")
    _stub_if_missing("langgraph.graph", StateGraph=object, START="START", END="END")
    _stub_if_missing("langgraph.graph.message", add_messages=lambda *a, **k: None)
    _stub_if_missing("langgraph.errors",
                     GraphRecursionError=_FakeGraphRecursionError,
                     NodeError=_FakeNodeError)
    _stub_if_missing("langgraph.func", task=_fake_task)
    _stub_if_missing("langgraph.managed", RemainingSteps=int)
    _stub_if_missing("langgraph.types", RetryPolicy=_FakeRetryPolicy)
    # Subclases propias, NUNCA Exception a secas: _is_quota_error hace
    # isinstance(exc, RateLimitError) — con Exception, todo error contaría
    # como error de cuota y el fallback engulliría bugs reales.
    _stub_if_missing("openai", RateLimitError=_FakeRateLimitError, APIStatusError=_FakeAPIStatusError)
    _stub_if_missing("atlassian", Jira=object)
    _force_stub("langchain_openai", ChatOpenAI=_FakeChatOpenAI)


def load_real_langgraph_fleet(alias: str):
    """Carga agile_scripts/langgraph_fleet.py bajo `alias` con stubs instalados."""
    install_stubs()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scripts_dir = os.path.join(repo_root, "agile_scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)  # para `import fleet_tracing`
    module_path = os.path.join(scripts_dir, "langgraph_fleet.py")
    spec = importlib.util.spec_from_file_location(alias, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
