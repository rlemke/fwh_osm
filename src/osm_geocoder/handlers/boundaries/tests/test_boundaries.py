#!/usr/bin/env python3
"""End-to-end test for the boundary extraction handlers.

Demonstrates the boundary extraction workflow with a mock handler
(no real PBF parsing). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_boundaries.py
"""

from facetwork.runtime import Evaluator, ExecutionStatus, MemoryStore, Telemetry
from facetwork.runtime.agent_poller import AgentPoller, AgentPollerConfig

# Runtime AST for BoundaryFeatures schema and event facets
PROGRAM_AST = {
    "type": "Program",
    "declarations": [
        {
            "type": "SchemaDecl",
            "name": "OSMCache",
            "fields": [
                {"name": "url", "type": "String"},
                {"name": "path", "type": "String"},
                {"name": "date", "type": "String"},
                {"name": "size", "type": "Long"},
                {"name": "wasInCache", "type": "Boolean"},
            ],
        },
        {
            "type": "SchemaDecl",
            "name": "BoundaryFeatures",
            "fields": [
                {"name": "output_path", "type": "String"},
                {"name": "feature_count", "type": "Long"},
                {"name": "boundary_type", "type": "String"},
                {"name": "admin_levels", "type": "String"},
                {"name": "format", "type": "String"},
                {"name": "extraction_date", "type": "String"},
            ],
        },
        {
            "type": "Namespace",
            "name": "osm",
            "declarations": [
                {
                    "type": "Namespace",
                    "name": "geo",
                    "declarations": [
                        {
                            "type": "Namespace",
                            "name": "Boundaries",
                            "declarations": [
                                {
                                    "type": "EventFacetDecl",
                                    "name": "CountryBoundaries",
                                    "params": [{"name": "cache", "type": "OSMCache"}],
                                    "returns": [{"name": "result", "type": "BoundaryFeatures"}],
                                },
                                {
                                    "type": "EventFacetDecl",
                                    "name": "AdminBoundary",
                                    "params": [
                                        {"name": "cache", "type": "OSMCache"},
                                        {
                                            "name": "admin_level",
                                            "type": "Long",
                                            "default": 2,
                                        },
                                    ],
                                    "returns": [{"name": "result", "type": "BoundaryFeatures"}],
                                },
                                {
                                    "type": "EventFacetDecl",
                                    "name": "LakeBoundaries",
                                    "params": [{"name": "cache", "type": "OSMCache"}],
                                    "returns": [{"name": "result", "type": "BoundaryFeatures"}],
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}

# Workflow that extracts country boundaries from a cached PBF
WORKFLOW_AST = {
    "type": "WorkflowDecl",
    "name": "ExtractCountryBoundaries",
    "params": [{"name": "cache", "type": "OSMCache"}],
    "returns": [{"name": "boundaries", "type": "BoundaryFeatures"}],
    "body": {
        "type": "AndThenBlock",
        "steps": [
            {
                "type": "StepStmt",
                "id": "step-extract",
                "name": "extract",
                "call": {
                    "type": "CallExpr",
                    "target": "CountryBoundaries",
                    "args": [
                        {
                            "name": "cache",
                            "value": {"type": "InputRef", "path": ["cache"]},
                        }
                    ],
                },
            },
        ],
        "yield": {
            "type": "YieldStmt",
            "id": "yield-ExtractCountryBoundaries",
            "call": {
                "type": "CallExpr",
                "target": "ExtractCountryBoundaries",
                "args": [
                    {
                        "name": "boundaries",
                        "value": {"type": "StepRef", "path": ["extract", "result"]},
                    }
                ],
            },
        },
    },
}


def mock_country_boundaries_handler(payload: dict) -> dict:
    """Mock handler that returns a simulated boundary extraction result."""
    _cache = payload.get("cache", {})
    return {
        "result": {
            "output_path": "/tmp/osm-boundaries/test_admin2.geojson",
            "feature_count": 42,
            "boundary_type": "country",
            "admin_levels": "2",
            "format": "GeoJSON",
            "extraction_date": "2026-02-03T12:00:00Z",
        }
    }


def main() -> None:
    """Run the boundary extraction workflow end-to-end with a mock handler."""
    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # Set up agent with mock handler
    poller = AgentPoller(
        persistence=store,
        evaluator=evaluator,
        config=AgentPollerConfig(service_name="test-boundaries"),
    )
    poller.register("osm.Boundaries.CountryBoundaries", mock_country_boundaries_handler)

    # Test input: a cached PBF file
    test_cache = {
        "url": "https://download.geofabrik.de/europe/monaco-latest.osm.pbf",
        "path": "/tmp/osm-cache/monaco-latest.osm.pbf",
        "date": "2026-02-03",
        "size": 1024000,
        "wasInCache": True,
    }

    # 1. Execute workflow - pauses at the CountryBoundaries event step
    print("Executing ExtractCountryBoundaries workflow...")
    result = evaluator.execute(
        WORKFLOW_AST,
        inputs={"cache": test_cache},
        program_ast=PROGRAM_AST,
    )
    print(f"  Status: {result.status}")
    assert result.status == ExecutionStatus.PAUSED, "Should pause at event step"

    # 2. Cache AST so the agent can resume the workflow
    poller.cache_workflow_ast(result.workflow_id, WORKFLOW_AST)

    # 3. Agent processes the event
    print("Agent processing CountryBoundaries event...")
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
    boundaries = final.outputs.get("boundaries", {})
    assert boundaries.get("feature_count") == 42
    assert boundaries.get("boundary_type") == "country"
    assert boundaries.get("format") == "GeoJSON"

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
