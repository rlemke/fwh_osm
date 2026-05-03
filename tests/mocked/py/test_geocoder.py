#!/usr/bin/env python3
"""Offline test for the OSM Geocoder agent.

Demonstrates the full workflow execution cycle with a mock geocode handler
(no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_geocoder.py
"""

from facetwork.runtime import Evaluator, ExecutionStatus, MemoryStore, Telemetry
from facetwork.runtime.agent_poller import AgentPoller, AgentPollerConfig

# Runtime AST for:
#
#   namespace osm.geocode {
#       event Geocode(address: String) => (result: GeoCoordinate)
#   }
#
PROGRAM_AST = {
    "type": "Program",
    "declarations": [
        {
            "type": "Namespace",
            "name": "osm",
            "declarations": [
                {
                    "type": "Namespace",
                    "name": "geo",
                    "declarations": [
                        {
                            "type": "EventFacetDecl",
                            "name": "Geocode",
                            "params": [{"name": "address", "type": "String"}],
                            "returns": [{"name": "result", "type": "GeoCoordinate"}],
                        },
                    ],
                },
            ],
        },
    ],
}

# Runtime AST for:
#
#   workflow GeocodeAddress(address: String) => (location: GeoCoordinate) andThen {
#       geo = Geocode(address = $.address)
#       yield GeocodeAddress(location = geo.result)
#   }
#
WORKFLOW_AST = {
    "type": "WorkflowDecl",
    "name": "GeocodeAddress",
    "params": [{"name": "address", "type": "String"}],
    "returns": [{"name": "location", "type": "GeoCoordinate"}],
    "body": {
        "type": "AndThenBlock",
        "steps": [
            {
                "type": "StepStmt",
                "id": "step-geocode",
                "name": "geo",
                "call": {
                    "type": "CallExpr",
                    "target": "Geocode",
                    "args": [
                        {
                            "name": "address",
                            "value": {"type": "InputRef", "path": ["address"]},
                        }
                    ],
                },
            },
        ],
        "yield": {
            "type": "YieldStmt",
            "id": "yield-GeocodeAddress",
            "call": {
                "type": "CallExpr",
                "target": "GeocodeAddress",
                "args": [
                    {
                        "name": "location",
                        "value": {"type": "StepRef", "path": ["geo", "result"]},
                    }
                ],
            },
        },
    },
}


def mock_geocode_handler(payload: dict) -> dict:
    """Mock handler that returns fixed coordinates for any address."""
    return {
        "result": {
            "lat": "48.8566",
            "lon": "2.3522",
            "display_name": f"Mock result for: {payload['address']}",
        }
    }


def main() -> None:
    """Run the geocoder workflow end-to-end with a mock handler."""
    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # Set up agent with mock handler
    poller = AgentPoller(
        persistence=store,
        evaluator=evaluator,
        config=AgentPollerConfig(service_name="test-geocoder"),
    )
    poller.register("osm.Geocode", mock_geocode_handler)

    # 1. Execute workflow — pauses at the Geocode event step
    print("Executing GeocodeAddress workflow...")
    result = evaluator.execute(
        WORKFLOW_AST,
        inputs={"address": "1600 Pennsylvania Avenue, Washington DC"},
        program_ast=PROGRAM_AST,
    )
    print(f"  Status: {result.status}")
    assert result.status == ExecutionStatus.PAUSED, "Should pause at event step"

    # 2. Cache AST so the agent can resume the workflow
    poller.cache_workflow_ast(result.workflow_id, WORKFLOW_AST)

    # 3. Agent processes the event
    print("Agent processing Geocode event...")
    dispatched = poller.poll_once()
    print(f"  Dispatched: {dispatched} task(s)")
    assert dispatched == 1

    # 4. Resume workflow to completion
    print("Resuming workflow...")
    final = evaluator.resume(result.workflow_id, WORKFLOW_AST, PROGRAM_AST)
    print(f"  Status: {final.status}")
    print(f"  Outputs: {final.outputs}")

    assert final.success
    assert final.status == ExecutionStatus.COMPLETED
    location = final.outputs.get("location", {})
    assert location.get("lat") == "48.8566"
    assert location.get("lon") == "2.3522"

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
