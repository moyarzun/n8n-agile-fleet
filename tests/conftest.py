"""
Pytest configuration: stub out heavy runtime dependencies so unit tests
can import fleet_api without a full Docker environment.
"""
import sys
import types
from unittest.mock import MagicMock
import pytest


def _make_mock_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__spec__ = None
    # Marca para tests/_real_fleet_loader.py: estos módulos son stubs y pueden
    # recibir atributos adicionales (nunca se debe mutar un módulo real).
    mod.__fleet_test_stub__ = True
    return mod


# Stub de las dependencias transitivas SOLO si no están instaladas de verdad
# (en el contenedor sí lo están, y pisarlas con mocks rompería los tests que
# usan el langgraph real vía importorskip). langgraph_fleet se mockea SIEMPRE:
# fleet_api debe importar la versión controlable por los tests.
import importlib as _importlib

for mod_name in (
    "langchain_core",
    "langchain_core.messages",
    "langchain_openai",
    "langgraph",
    "langgraph.graph",
):
    if mod_name in sys.modules:
        continue
    try:
        _importlib.import_module(mod_name)
        continue  # módulo real disponible: no stubear
    except ImportError:
        sys.modules[mod_name] = _make_mock_module(mod_name)

if "langgraph_fleet" not in sys.modules:
    sys.modules["langgraph_fleet"] = _make_mock_module("langgraph_fleet")

# Provide the symbols fleet_api.py pulls from langgraph_fleet
mock_fleet = sys.modules["langgraph_fleet"]
class _MockGraphRecursionError(Exception):
    """Clase real (no MagicMock): fleet_api la usa en un `except`."""


mock_fleet.build_architecture = MagicMock()
mock_fleet.FleetState = dict
mock_fleet.stop_gracefully = MagicMock()
mock_fleet.set_log_callback = MagicMock()
mock_fleet.invoke_config = lambda job_id: {"configurable": {"thread_id": job_id}, "recursion_limit": 60}
mock_fleet.delete_job_checkpoints = MagicMock()
mock_fleet.GraphRecursionError = _MockGraphRecursionError


import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))

# fleet_api._DB_PATH se fija al importar el módulo (default: ruta de Docker
# /data/n8n_store/fleet.db, que no existe fuera del contenedor). Redirigirla
# a un archivo temporario ANTES del primer import de fleet_api para que la
# suite corra igual dentro y fuera de Docker.
os.environ.setdefault("FLEET_DB", os.path.join(tempfile.gettempdir(), "fleet_test.db"))

# El schema de sqlite se crea en el handler @app.on_event("startup"), que
# starlette.TestClient solo dispara si se usa como context manager (`with
# TestClient(app) as c`). Varios tests usan TestClient(app) directo (sin
# `with`), así que garantizamos acá que la tabla exista sin depender de eso.
import fleet_api as _fleet_api_bootstrap  # noqa: E402
_fleet_api_bootstrap._init_db()


@pytest.fixture(autouse=True)
def clear_jobs():
    import fleet_api
    fleet_api._jobs.clear()
    yield
    fleet_api._jobs.clear()
