"""Distributed integration tests: client-only, Docker stack does the work.

These tests exercise the real distributed architecture:
  Client (this test) → MongoDB → RunnerService (Docker) → Agent (Docker) → completion

The test only acts as a client: compile, store flow, submit task, and poll for
completion. The Docker runner and agent services handle all execution.

Prerequisites:
    - Docker stack running: scripts/setup --build --runners 1 --agents 1
    - MongoDB accessible at afl-mongodb:27017

Run:
    # Simple tests (only need runner + AddOne agent)
    pytest examples/osm-geocoder/tests/real/py/test_distributed.py -v --mongodb

    # Full city routing (needs runner + OSM agent)
    scripts/setup --runners 1 --agents 1 --osm-agents 1
    pytest examples/osm-geocoder/tests/real/py/test_distributed.py -v --mongodb
"""

import pytest
from helpers import (
    EXAMPLE_AFL_FILES,
    store_flow,
    submit_workflow,
    wait_for_task,
)

# MongoDB server (external, defined in /etc/hosts)
DOCKER_MONGODB_URL = "mongodb://afl-mongodb:27017"
DOCKER_DATABASE = "facetwork"


@pytest.fixture
def docker_db():
    """Connect to MongoDB at afl-mongodb:27017."""
    pymongo = pytest.importorskip("pymongo", reason="pymongo required for distributed tests")
    client = pymongo.MongoClient(DOCKER_MONGODB_URL, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("MongoDB not reachable at afl-mongodb:27017")
    db = client[DOCKER_DATABASE]
    yield db
    client.close()


# ---------------------------------------------------------------------------
# FFL source for simple AddOne tests (inline, self-contained)
# ---------------------------------------------------------------------------

ADDONE_SOURCE = """\
namespace handlers {
    event facet AddOne(value: Long) => (result: Long)

    workflow AddOneWorkflow(input: Long) => (output: Long) andThen {
        added = AddOne(value = $.input)
        yield AddOneWorkflow(output = added.result)
    }
}
"""

CHAIN_SOURCE = """\
namespace handlers {
    event facet AddOne(value: Long) => (result: Long)
}

namespace chain {
    use handlers

    workflow ChainOfThree(start: Long) => (final: Long) andThen {
        step1 = handlers.AddOne(value = $.start)
        step2 = handlers.AddOne(value = step1.result)
        step3 = handlers.AddOne(value = step2.result)
        yield ChainOfThree(final = step3.result)
    }
}
"""

# FFL library files needed by osmcityrouting.afl
_CITY_ROUTING_LIBRARIES = [
    "osmtypes.ffl",
    "osmregion.ffl",
    "osmoperations.ffl",
    "osmgraphhopper.ffl",
    "osmfilters_population.ffl",
    "osmvisualization.ffl",
]


def _has_running_server(db, service_pattern: str) -> bool:
    """Check if any server matching the pattern has a recent heartbeat."""
    import time

    now_ms = int(time.time() * 1000)
    # Consider servers with a heartbeat within the last 30 seconds
    cutoff = now_ms - 30_000
    server = db.servers.find_one(
        {
            "service_name": {"$regex": service_pattern},
            "ping_time": {"$gt": cutoff},
        }
    )
    return server is not None


def _wait_for_runner_state(db, task_doc: dict, expected_states: set[str], timeout_s: int = 30):
    """Find the runner associated with a completed task and verify its state.

    The runner's workflow_id is set by _handle_execute_workflow after execution.
    We find it by looking at runners whose workflow_id matches any workflow
    referenced by steps in this task's flow.
    """
    import time

    # The task data has flow_id; the runner is linked by workflow_id.
    # After the task completes, the runner should exist. We find it by
    # looking for runners created around the same time.
    data = task_doc.get("data", {})
    flow_id = data.get("flow_id", "")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # Find runners that reference this flow's workflows
        runners = list(db.runners.find({"state": {"$in": list(expected_states)}}))
        for runner in runners:
            # Match by checking if the runner's workflow data references our flow
            wf = runner.get("workflow", {})
            if wf.get("flow_id") == flow_id:
                return runner
        time.sleep(1)

    return None


@pytest.mark.distributed
class TestDistributed:
    """Tests that use the Docker stack for distributed execution."""

    def test_addone_distributed(self, docker_db):
        """AddOne(41) => 42 via Docker runner + agent."""
        flow_id = store_flow(
            docker_db,
            "test-addone-distributed",
            [("addone.ffl", ADDONE_SOURCE)],
        )

        task_id = submit_workflow(
            docker_db,
            flow_id,
            "handlers.AddOneWorkflow",
            inputs={"input": 41},
        )

        task_doc = wait_for_task(docker_db, task_id, timeout_s=30)
        assert task_doc["state"] == "completed", f"Task failed: {task_doc.get('error')}"

    def test_chain_distributed(self, docker_db):
        """3-step chain: start + 3 via Docker runner + agent resume cycle."""
        flow_id = store_flow(
            docker_db,
            "test-chain-distributed",
            [("chain.ffl", CHAIN_SOURCE)],
        )

        start_value = 10
        task_id = submit_workflow(
            docker_db,
            flow_id,
            "chain.ChainOfThree",
            inputs={"start": start_value},
        )

        task_doc = wait_for_task(docker_db, task_id, timeout_s=60)
        assert task_doc["state"] == "completed", f"Task failed: {task_doc.get('error')}"

        # Verify the runner completed with correct output
        runner = _wait_for_runner_state(docker_db, task_doc, {"completed"}, timeout_s=10)
        if runner:
            assert runner["state"] == "completed"

    def test_city_routing_distributed(self, docker_db):
        """Full 9-step CityRouteMap pipeline via Docker runner + OSM agent.

        Requires the OSM geocoder agent to be running in the Docker stack:
            scripts/setup --runners 1 --agents 1 --osm-agents 1
        """
        # Skip if no OSM agent is registered
        if not _has_running_server(docker_db, "osm"):
            pytest.skip(
                "No OSM geocoder agent running in Docker stack. "
                "Start with: scripts/setup --osm-agents 1"
            )

        # Read all FFL source files and concatenate into one string
        # (RunnerService parses compiled_sources[0].content as a single file)
        primary_path = EXAMPLE_AFL_FILES["osmcityrouting.ffl"]
        lib_paths = [EXAMPLE_AFL_FILES[f] for f in _CITY_ROUTING_LIBRARIES]

        all_sources = []
        for p in [*lib_paths, primary_path]:
            all_sources.append(p.read_text())
        combined_source = "\n".join(all_sources)

        flow_id = store_flow(
            docker_db,
            "test-cityrouting-distributed",
            [("osmcityrouting-combined.ffl", combined_source)],
        )

        task_id = submit_workflow(
            docker_db,
            flow_id,
            "osm.CityRouteMap",
            inputs={
                "region": "germany",
                "minPopulation": 500000,
                "maxPopulation": 5000000,
                "routingProfile": "car",
            },
        )

        # City routing involves network downloads — allow generous timeout
        task_doc = wait_for_task(docker_db, task_id, timeout_s=300)
        assert task_doc["state"] == "completed", f"Task failed: {task_doc.get('error')}"
