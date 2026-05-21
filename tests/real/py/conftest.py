"""Shared fixtures for integration tests.

All tests in this directory require a running MongoDB instance.
Run with: pytest examples/osm-geocoder/tests/real/py/ -v --mongodb
"""

import os

import pytest

# tests/real/py/ → repo root (this package). The package is installed via
# pyproject.toml, so handlers are imported package-qualified
# (``osm_geocoder.handlers.*``) — no sys.path manipulation or stale-module
# purging is needed. ``_THIS_DIR`` is kept only to scope the --mongodb skip
# below to this directory.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# NOTE: the former ``_ensure_osm_handlers()`` hack (which purged a bare
# ``handlers`` top-level module and prepended example roots onto sys.path) was
# a monorepo leftover — it never matched the ``osm_geocoder`` package layout
# and is unnecessary now that handlers are package-qualified. It has been
# removed; the --mongodb skip and the mongo_store / evaluator / poller fixtures
# below are preserved.

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
