"""Shared fixtures for integration tests.

All tests in this directory require a running MongoDB instance.
Run with: pytest examples/osm-geocoder/tests/real/py/ -v --mongodb
"""

import os
import sys

import pytest

# examples/osm-geocoder/tests/real/py/ → examples/osm-geocoder/
_EXAMPLE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _ensure_osm_handlers():
    """Purge stale handlers and ensure osm-geocoder handlers are active."""
    for key in list(sys.modules.keys()):
        if key == "handlers" or key.startswith("handlers."):
            mod = sys.modules[key]
            mod_file = getattr(mod, "__file__", "") or ""
            if "osm-geocoder" not in mod_file:
                del sys.modules[key]
    if _EXAMPLE_ROOT in sys.path:
        sys.path.remove(_EXAMPLE_ROOT)
    sys.path.insert(0, _EXAMPLE_ROOT)
    if _THIS_DIR not in sys.path:
        sys.path.insert(0, _THIS_DIR)


# Purge at collection time
_ensure_osm_handlers()


@pytest.fixture(autouse=True)
def _osm_handlers_on_path():
    """Ensure osm-geocoder handlers are on sys.path before each test."""
    _ensure_osm_handlers()


from facetwork.runtime import Evaluator, Telemetry
from facetwork.runtime.agent_poller import AgentPoller, AgentPollerConfig


def _use_real_mongodb(request) -> bool:
    """Check if --mongodb flag was passed."""
    return request.config.getoption("--mongodb", default=False)


# Skip tests in this directory if --mongodb not passed
def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --mongodb is provided."""
    if not config.getoption("--mongodb", default=False):
        skip = pytest.mark.skip(reason="Integration tests require --mongodb flag")
        this_dir = os.path.dirname(os.path.abspath(__file__))
        for item in items:
            if str(item.fspath).startswith(this_dir):
                item.add_marker(skip)


@pytest.fixture
def mongo_store(request):
    """Create a MongoStore backed by a real MongoDB server.

    Uses FFL config for connection settings. Database is dropped after each test.
    """
    from facetwork.config import load_config
    from facetwork.runtime.mongo_store import MongoStore

    config = load_config()
    store = MongoStore(
        connection_string=config.mongodb.connection_string(),
        database_name="afl_integration_test",
    )
    yield store
    store.drop_database()
    store.close()


@pytest.fixture
def evaluator(mongo_store):
    """Create an Evaluator backed by MongoDB."""
    return Evaluator(persistence=mongo_store, telemetry=Telemetry(enabled=False))


@pytest.fixture
def poller(mongo_store, evaluator):
    """Create an AgentPoller with no handlers registered.

    Tests should register their own handlers before use.
    """
    return AgentPoller(
        persistence=mongo_store,
        evaluator=evaluator,
        config=AgentPollerConfig(service_name="integration-test"),
    )
