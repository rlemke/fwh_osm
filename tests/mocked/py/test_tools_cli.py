"""Offline CLI-contract tests for the osm_geocoder tools (``src/osm_geocoder/tools/*.py``).

These tools are argparse-based command-line scripts that drive real work
(downloads, osmium/tippecanoe/postgis, file I/O). They previously had **no
dedicated tests**. This module asserts only the *argument-parsing contract* and
deliberately never lets a tool do real work:

  * ``--help`` exits 0 and prints a ``usage:`` line (proves the tool imports
    cleanly and its parser is wired up — fully offline, no heavy deps fire).
  * a tool whose parser declares required arguments exits with code 2 and writes
    an error to stderr when invoked with no arguments (argparse's standard
    "missing required" path — it bails *before* any real work).

Tools are discovered dynamically from the installed package, so a newly added
tool is automatically covered. Each tool runs in its own subprocess (the same
interpreter running the tests), so a module-level import failure surfaces as a
clean test failure rather than poisoning the test process.

The suite is offline: ``--help`` and the missing-required-args path never reach
network, MongoDB, or external binaries.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Discover the tools directory from the *importable* package so the tests work
# regardless of the checkout layout (src/ vs installed).
# ---------------------------------------------------------------------------
try:
    import osm_geocoder

    _TOOLS_DIR = Path(osm_geocoder.__file__).resolve().parent / "tools"
except Exception:  # pragma: no cover - package must be importable to test it
    _TOOLS_DIR = None


def _discover_tools() -> list[Path]:
    """Return the argparse-based Python CLI scripts under tools/.

    A script qualifies if it imports ``argparse`` and has a ``__main__`` guard
    (i.e. it is an executable CLI, not a helper module). Files starting with an
    underscore (e.g. package dirs / private helpers) are skipped.
    """
    if _TOOLS_DIR is None or not _TOOLS_DIR.is_dir():
        return []
    tools: list[Path] = []
    for p in sorted(_TOOLS_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "argparse" in text and "__main__" in text:
            tools.append(p)
    return tools


_TOOLS = _discover_tools()
_TOOL_IDS = [p.name for p in _TOOLS]


def _run(tool: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Run ``python <tool> <args>`` offline and capture its result.

    cwd is the tool's own directory so any sibling-relative imports resolve.
    """
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True,
        text=True,
        cwd=str(tool.parent),
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


def _has_required_args(help_stdout: str) -> bool:
    """True if the --help usage line shows a required argument.

    argparse wraps *optional* arguments in ``[...]`` in the usage string; a
    required option or positional appears bare. We read the usage block from the
    (offline, safe) ``--help`` output and look for any token outside brackets
    beyond the program name and the implicit ``[-h]``. This lets us decide
    whether running the tool with no args is safe to assert on *without* ever
    executing the tool's real work (some all-optional tools, e.g. an HTTP
    server, would block / act on a no-args run).
    """
    lines = help_stdout.splitlines()
    # The usage block starts at "usage:" and continues over indented wrap lines.
    usage: list[str] = []
    in_usage = False
    for line in lines:
        if line.lower().startswith("usage:"):
            in_usage = True
            usage.append(line)
            continue
        if in_usage:
            if line.startswith((" ", "\t")) and line.strip():
                usage.append(line)
            else:
                break
    text = " ".join(usage)
    # Strip "usage:" and the program name (first token after it).
    text = text[len("usage:"):].strip() if text.lower().startswith("usage:") else text
    # Remove everything inside [...] (optionals) and (...) groups' brackets,
    # then drop the program name token; anything left is a required arg.
    # Mutually-exclusive required groups render as (a | b) — keep those.
    no_optionals = re.sub(r"\[[^\[\]]*\]", " ", text)
    # collapse and drop the leading program-name token
    tokens = no_optionals.split()
    tokens = tokens[1:] if tokens else tokens  # drop prog name
    leftover = " ".join(tokens).strip()
    return bool(leftover)


def test_tools_discovered():
    """Sanity: we actually found the tools dir and some CLI scripts.

    Guards against the discovery silently returning [] (which would make every
    parametrized test vacuously pass / skip).
    """
    assert _TOOLS_DIR is not None, "osm_geocoder package is not importable"
    assert _TOOLS, f"no argparse CLI tools discovered under {_TOOLS_DIR}"


@pytest.mark.parametrize("tool", _TOOLS, ids=_TOOL_IDS)
def test_help_exits_zero_with_usage(tool: Path):
    """``<tool> --help`` exits 0 and prints a usage line, fully offline.

    This also proves the tool's module imports cleanly on this host (all
    top-level imports resolve) without firing any real work.
    """
    proc = _run(tool, "--help")
    assert proc.returncode == 0, (
        f"{tool.name} --help exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "usage:" in proc.stdout.lower(), (
        f"{tool.name} --help printed no usage line\nstdout:\n{proc.stdout}"
    )


@pytest.mark.parametrize("tool", _TOOLS, ids=_TOOL_IDS)
def test_missing_required_args_exits_2(tool: Path):
    """Tools with required args reject a no-args run (exit 2 + stderr message).

    Requiredness is determined from the tool's own ``--help`` usage line (read
    offline, no real work). Tools whose arguments are *all* optional are skipped:
    invoking them with no args would attempt real work (downloads / osmium /
    file I/O, or — like serve-html-maps.py — start a server and block), which
    this offline suite must not do. Only when --help shows a required argument do
    we run the no-args path and assert argparse's standard exit-2 + usage error,
    which fires *before* any real work.
    """
    help_proc = _run(tool, "--help")
    if help_proc.returncode != 0 or not _has_required_args(help_proc.stdout):
        pytest.skip(
            f"{tool.name} has no required args; a no-args run would do real work "
            "(offline suite must not invoke downloads/osmium/postgis/servers)"
        )

    # Short timeout: a tool with required args must bail at parse time well
    # before this; a hang would itself be a contract violation.
    proc = _run(tool, timeout=20)
    stderr = proc.stderr or ""
    assert proc.returncode == 2, (
        f"{tool.name} with no args should exit 2 (argparse usage error), "
        f"got {proc.returncode}\nstderr:\n{stderr}"
    )
    assert "the following arguments are required" in stderr, (
        f"{tool.name} no-args run should report missing required args\nstderr:\n{stderr}"
    )
    assert "usage:" in stderr.lower(), (
        f"{tool.name} no-args error should include a usage line\nstderr:\n{stderr}"
    )
