"""Integration test: compile AddOne from FFL source, run through MongoDB + AgentPoller.

This is the simplest integration test. It proves the full pipeline:
  FFL file → compile → MongoStore → Evaluator → AgentPoller → handler → completion

No external dependencies beyond MongoDB.

Run:
    pytest examples/osm-geocoder/tests/real/py/test_addone.py -v --mongodb
"""

from helpers import (
    INTEGRATION_AFL_DIR,
    compile_afl_files,
    extract_workflow,
    run_to_completion,
)

from facetwork.runtime import ExecutionStatus


def addone_handler(payload: dict) -> dict:
    """Handle AddOne event: output = input + 1."""
    return {"output": payload["input"] + 1}


class TestAddOneIntegration:
    """End-to-end: FFL source → MongoDB → AgentPoller → handler → result."""

    def test_compile_addone_afl(self):
        """The addone.afl file compiles without errors."""
        program = compile_afl_files(INTEGRATION_AFL_DIR / "addone.ffl")
        assert program["type"] == "Program"

        workflow = extract_workflow(program, "TestAddOne")
        assert workflow["name"] == "TestAddOne"
        assert workflow["type"] == "WorkflowDecl"

    def test_addone_compiled_from_afl(self, mongo_store, evaluator, poller):
        """Compile from file, execute, assert result=2 for input=1."""
        program = compile_afl_files(INTEGRATION_AFL_DIR / "addone.ffl")
        workflow = extract_workflow(program, "TestAddOne")

        poller.register("handlers.AddOne", addone_handler)

        result = run_to_completion(evaluator, poller, workflow, program, inputs={"x": 1})

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert result.outputs["result"] == 2

    def test_addone_different_input(self, mongo_store, evaluator, poller):
        """AddOne(input=41) => output=42 via full pipeline."""
        program = compile_afl_files(INTEGRATION_AFL_DIR / "addone.ffl")
        workflow = extract_workflow(program, "TestAddOne")

        poller.register("handlers.AddOne", addone_handler)

        result = run_to_completion(evaluator, poller, workflow, program, inputs={"x": 41})

        assert result.success
        assert result.outputs["result"] == 42

    def test_addone_mongodb_round_trip(self, mongo_store, evaluator, poller):
        """Verify steps are persisted in MongoDB after execution."""
        program = compile_afl_files(INTEGRATION_AFL_DIR / "addone.ffl")
        workflow = extract_workflow(program, "TestAddOne")

        poller.register("handlers.AddOne", addone_handler)

        result = run_to_completion(evaluator, poller, workflow, program, inputs={"x": 5})

        assert result.success
        assert result.outputs["result"] == 6

        # Verify steps exist in the MongoDB-backed store
        steps = list(mongo_store.get_steps_by_workflow(result.workflow_id))
        assert len(steps) > 0

        # Find the AddOne event facet step and verify its attributes
        addone_steps = [s for s in steps if s.facet_name == "handlers.AddOne"]
        assert len(addone_steps) == 1
        assert addone_steps[0].attributes.get_param("input") == 5
        assert addone_steps[0].attributes.get_param("output") == 6
