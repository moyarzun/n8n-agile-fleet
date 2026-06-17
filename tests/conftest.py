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


import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agile_scripts'))


@pytest.fixture(autouse=True)
def clear_jobs():
    import fleet_api
    fleet_api._jobs.clear()
    yield
    fleet_api._jobs.clear()
