"""
Pytest configuration: stub out heavy runtime dependencies so unit tests
can import fleet_api without a full Docker environment.
"""
import sys
import types
from unittest.mock import MagicMock


def _make_mock_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__spec__ = None
    return mod


# Stub langgraph_fleet and its transitive deps before any test import
for mod_name in (
    "langchain_core",
    "langchain_core.messages",
    "langchain_openai",
    "langgraph",
    "langgraph.graph",
    "langgraph_fleet",
):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = _make_mock_module(mod_name)

# Provide the symbols fleet_api.py pulls from langgraph_fleet
mock_fleet = sys.modules["langgraph_fleet"]
mock_fleet.build_architecture = MagicMock()
mock_fleet.FleetState = dict
