"""Cooperative cancellation for the long osmium passes.

Facetwork cancellation is cooperative — a handler that never checks runs to
completion, because Python cannot safely kill a thread blocked in a C call. For
this domain "blocked in a C call" is really "blocked in a subprocess": an
``osmium`` pass over a continental extract runs for tens of minutes.

Measured 2026-08-25: after ``fw maint terminate-workflow`` marked a europe
admin-split terminal, **both** hosts kept burning CPU on their osmium passes
until the containers were restarted by hand. Terminating the run did not
terminate the work.

So checking a flag between phases is not enough here — the phase itself has to
be interruptible. :func:`run_cancellable` polls the child and kills its PROCESS
GROUP (osmium spawns helpers; signalling only the direct child orphans them).

The active predicate is **thread-local**, not module-global: a runner executes
several tasks concurrently on a thread pool, and a global would let one task's
cancellation abort another's osmium pass.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator

try:  # the runtime owns the exception type, so a cancelled handler is a CLEAN
    # stop (no retry, no failed step) rather than an error the runner retries.
    from facetwork.runtime.cancellation import HandlerCancelled
except Exception:  # pragma: no cover - tools are usable without the runtime
    class HandlerCancelled(Exception):  # type: ignore[no-redef]
        """Fallback when the runtime is not importable (CLI/tool use)."""


_state = threading.local()

# How often to look at the child while it runs. Cheap (one waitpid poll), and
# well under the time a human waits after pressing terminate.
_POLL_S = 2.0
# Grace between SIGTERM and SIGKILL for the process group.
_KILL_GRACE_S = 5.0


def _predicate() -> Callable[[], str | None] | None:
    return getattr(_state, "predicate", None)


@contextlib.contextmanager
def cancellable(predicate: Callable[[], str | None] | None) -> Iterator[None]:
    """Make ``predicate`` the cancellation source for THIS thread.

    ``predicate`` returns a reason string when the work should stop, else None —
    the same shape the runtime injects as ``_cancellation_check``. Passing None
    disables cancellation, which is the behaviour every direct/CLI caller had
    before this existed.
    """
    previous = _predicate()
    _state.predicate = predicate
    try:
        yield
    finally:
        _state.predicate = previous


def cancelled_reason() -> str | None:
    """Reason this execution should stop, or None. Never raises."""
    pred = _predicate()
    if pred is None:
        return None
    try:
        return pred()
    except Exception:  # noqa: BLE001 - a broken check must not abort real work
        return None


def raise_if_cancelled() -> None:
    """Abort with :class:`HandlerCancelled` if this execution was cancelled."""
    reason = cancelled_reason()
    if reason is not None:
        raise HandlerCancelled(reason)


def run_cancellable(cmd: list[str], **popen_kwargs) -> subprocess.CompletedProcess:
    """Run ``cmd`` to completion, aborting it if this execution is cancelled.

    Drop-in for ``subprocess.run(cmd, check=True)`` in the osmium paths. Raises
    :class:`HandlerCancelled` after killing the child's process group, or
    ``CalledProcessError`` on a non-zero exit, so existing error handling around
    the call is unchanged.
    """
    raise_if_cancelled()                       # do not even start if already cancelled
    if _predicate() is None:
        # No cancellation source (CLI, tests): keep the simple blocking path.
        return subprocess.run(cmd, check=True, **popen_kwargs)

    # start_new_session gives the child its own process group so we can signal
    # the whole tree; osmium's helpers would otherwise survive as orphans.
    proc = subprocess.Popen(cmd, start_new_session=True, **popen_kwargs)
    try:
        while True:
            try:
                proc.wait(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            reason = cancelled_reason()
            if reason is not None:
                _terminate_group(proc)
                raise HandlerCancelled(reason)
    finally:
        if proc.poll() is None:                # any other exit path (e.g. KeyboardInterrupt)
            _terminate_group(proc)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return subprocess.CompletedProcess(cmd, proc.returncode)


def _terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, then SIGKILL what survives."""
    for sig, wait in ((signal.SIGTERM, _KILL_GRACE_S), (signal.SIGKILL, 2.0)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return                              # already gone, or not ours to signal
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
