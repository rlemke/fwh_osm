"""Shared helpers for integration tests.

Provides compilation, workflow extraction, and execution helpers
that use real FFL source files and the full compiler pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymongo.database import Database

from facetwork.emitter import emit_dict
from facetwork.parser import FFLParser
from facetwork.runtime.agent_poller import AgentPoller
from facetwork.runtime.evaluator import Evaluator, ExecutionResult, ExecutionStatus
from facetwork.source import CompilerInput, FileOrigin, SourceEntry
from facetwork.validator import validate

# Paths
_EXAMPLE_ROOT = Path(__file__).parent.parent.parent.parent
EXAMPLE_AFL_DIR = _EXAMPLE_ROOT / "afl"  # root afl/ (geocoder.afl)
INTEGRATION_AFL_DIR = Path(__file__).parent.parent / "afl"

# All FFL files indexed by filename (root afl/ + handlers/*/ffl/)
EXAMPLE_AFL_FILES: dict[str, Path] = {}
for _p in sorted(_EXAMPLE_ROOT.rglob("*.ffl")):
    if "/tests/" not in str(_p):
        EXAMPLE_AFL_FILES[_p.name] = _p


def compile_afl_files(
    primary: str | Path,
    *libraries: str | Path,
) -> dict[str, Any]:
    """Compile FFL source files into a program dict.

    Args:
        primary: Path to the primary FFL source file
        *libraries: Paths to library FFL source files

    Returns:
        The full program dict (JSON-serializable AST)

    Raises:
        afl.parser.ParseError: On syntax errors
        ValueError: On validation errors
    """
    primary_path = Path(primary)
    primary_entry = SourceEntry(
        text=primary_path.read_text(),
        origin=FileOrigin(path=str(primary_path)),
    )

    lib_entries = []
    for lib in libraries:
        lib_path = Path(lib)
        lib_entries.append(
            SourceEntry(
                text=lib_path.read_text(),
                origin=FileOrigin(path=str(lib_path)),
                is_library=True,
            )
        )

    compiler_input = CompilerInput(
        primary_sources=[primary_entry],
        library_sources=lib_entries,
    )

    parser = FFLParser()
    program_ast, _registry = parser.parse_sources(compiler_input)

    result = validate(program_ast)
    if result.errors:
        messages = "; ".join(str(e) for e in result.errors)
        raise ValueError(f"Validation errors: {messages}")

    return emit_dict(program_ast, include_locations=False)


def extract_workflow(program_dict: dict, workflow_name: str) -> dict:
    """Find a WorkflowDecl by name in a compiled program dict.

    Searches recursively through namespaces and declarations.
    The emitted JSON uses both 'namespaces' (list of Namespace dicts)
    and 'workflows' (list of WorkflowDecl dicts) keys.

    Args:
        program_dict: The compiled program dict
        workflow_name: Name of the workflow to find

    Returns:
        The workflow dict

    Raises:
        KeyError: If the workflow is not found
    """

    def _search_node(node: dict) -> dict | None:
        # Check workflows list (emitter puts workflows here)
        for wf in node.get("workflows", []):
            if wf.get("name") == workflow_name:
                return wf
        # Check declarations list (alternative structure)
        for decl in node.get("declarations", []):
            if decl.get("type") == "WorkflowDecl" and decl.get("name") == workflow_name:
                return decl
            if decl.get("type") == "Namespace":
                found = _search_node(decl)
                if found:
                    return found
        # Recurse into namespaces list
        for ns in node.get("namespaces", []):
            found = _search_node(ns)
            if found:
                return found
        return None

    found = _search_node(program_dict)
    if found is None:
        raise KeyError(f"Workflow '{workflow_name}' not found in program")
    return found


def run_to_completion(
    evaluator: Evaluator,
    poller: AgentPoller,
    workflow_ast: dict,
    program_ast: dict,
    inputs: dict[str, Any] | None = None,
    max_rounds: int = 50,
) -> ExecutionResult:
    """Execute a workflow to completion through the AgentPoller pipeline.

    Loops: execute -> poll_once -> resume until COMPLETED, ERROR, or max_rounds.

    Args:
        evaluator: The Evaluator instance
        poller: AgentPoller with handlers registered
        workflow_ast: The workflow AST dict
        program_ast: The full program AST dict
        inputs: Workflow input parameters
        max_rounds: Maximum poll/resume cycles

    Returns:
        The final ExecutionResult
    """
    result = evaluator.execute(workflow_ast, inputs=inputs, program_ast=program_ast)

    if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.ERROR):
        return result

    poller.cache_workflow_ast(result.workflow_id, workflow_ast, program_ast=program_ast)

    for _ in range(max_rounds):
        dispatched = poller.poll_once()

        if dispatched == 0:
            # No tasks claimed — try resuming anyway (may have been continued already)
            pass

        result = evaluator.resume(result.workflow_id, workflow_ast, program_ast, inputs)

        if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.ERROR):
            return result

    return result


# =============================================================================
# Distributed Test Helpers
# =============================================================================


def store_flow(
    db: Database,
    name: str,
    afl_sources: list[tuple[str, str]],
) -> str:
    """Store a flow in MongoDB using MongoStore.save_flow().

    Creates a FlowDefinition with compiled_sources and saves it via the
    proper MongoStore serialization path so RunnerService can load it.

    Args:
        db: A pymongo Database instance (e.g. from MongoClient["afl"])
        name: Human-readable flow name
        afl_sources: List of (filename, source_text) tuples

    Returns:
        The flow's UUID string
    """
    import uuid as _uuid

    from facetwork.runtime.entities import FlowDefinition, FlowIdentity, SourceText
    from facetwork.runtime.mongo_store import MongoStore

    flow_id = str(_uuid.uuid4())
    flow = FlowDefinition(
        uuid=flow_id,
        name=FlowIdentity(name=name, path=f"/test/{name}", uuid=flow_id),
        compiled_sources=[
            SourceText(name=filename, content=content) for filename, content in afl_sources
        ],
    )

    # Create a MongoStore wrapping the existing db's client
    store = MongoStore(client=db.client, database_name=db.name, create_indexes=True)
    store.save_flow(flow)
    return flow_id


def submit_workflow(
    db: Database,
    flow_id: str,
    workflow_name: str,
    inputs: dict | None = None,
) -> str:
    """Submit an afl:execute task to the task queue.

    Creates a task document that RunnerService will pick up and execute.
    Uses task_list_name="default" to match RunnerService's default poll.

    Args:
        db: A pymongo Database instance
        flow_id: UUID of the flow to execute
        workflow_name: Qualified workflow name (e.g. "handlers.AddOneWorkflow")
        inputs: Workflow input parameters

    Returns:
        The task's UUID string
    """
    import time as _time
    import uuid as _uuid

    task_id = str(_uuid.uuid4())
    now_ms = int(_time.time() * 1000)

    task_doc = {
        "uuid": task_id,
        "name": "fw:execute",
        "runner_id": "",
        "workflow_id": "",
        "flow_id": flow_id,
        "step_id": "",
        "state": "pending",
        "created": now_ms,
        "updated": now_ms,
        "error": None,
        "task_list_name": "default",
        "data_type": "execute",
        "data": {
            "flow_id": flow_id,
            "workflow_name": workflow_name,
            "inputs": inputs or {},
        },
    }

    db.tasks.insert_one(task_doc)
    return task_id


def wait_for_task(
    db: Database,
    task_id: str,
    timeout_s: int = 120,
    poll_interval_s: float = 2.0,
) -> dict:
    """Poll the tasks collection until a task reaches a terminal state.

    Args:
        db: A pymongo Database instance
        task_id: UUID of the task to watch
        timeout_s: Maximum seconds to wait
        poll_interval_s: Seconds between polls

    Returns:
        The raw task document from MongoDB

    Raises:
        TimeoutError: If the task does not complete within timeout_s
    """
    import time as _time

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        doc = db.tasks.find_one({"uuid": task_id})
        if doc and doc.get("state") not in ("pending", "running"):
            return doc
        _time.sleep(poll_interval_s)

    # Fetch final state for the error message
    doc = db.tasks.find_one({"uuid": task_id})
    state = doc.get("state", "unknown") if doc else "not found"
    raise TimeoutError(f"Task {task_id} did not complete within {timeout_s}s (state={state})")
