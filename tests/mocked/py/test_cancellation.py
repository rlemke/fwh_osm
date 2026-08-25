"""Cancelling a run must actually stop the osmium pass.

Measured 2026-08-25: `fw maint terminate-workflow` marked a europe admin-split
terminal while BOTH hosts kept burning CPU on their osmium passes, until the
containers were restarted by hand. Terminating the run did not terminate the
work. Checking a flag between phases is not enough — the phase itself is a
subprocess that runs for tens of minutes, so it has to be interruptible.
"""
import os
import subprocess
import threading
import time

import pytest

from osm_geocoder.tools._osm_tools import cancellation as c


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(c, "_POLL_S", 0.05)
    monkeypatch.setattr(c, "_KILL_GRACE_S", 0.5)


def test_a_long_child_is_killed_when_cancelled(fast_poll):
    flag = {"stop": None}
    t0 = time.time()
    with c.cancellable(lambda: flag["stop"]):
        threading.Timer(0.3, lambda: flag.__setitem__("stop", "terminated")).start()
        with pytest.raises(c.HandlerCancelled):
            c.run_cancellable(["sleep", "60"])
    assert time.time() - t0 < 10, "cancel must not wait out the child"


def test_the_whole_process_GROUP_dies_not_just_the_child(fast_poll):
    """osmium spawns helpers; signalling only the direct child orphans them."""
    marker = f"/tmp/fw_cancel_probe_{os.getpid()}"
    # parent spawns a grandchild that would outlive it, then sleeps
    script = f"sh -c 'sleep 60 & echo $! > {marker}; sleep 60'"
    flag = {"stop": None}
    with c.cancellable(lambda: flag["stop"]):
        threading.Timer(0.5, lambda: flag.__setitem__("stop", "terminated")).start()
        with pytest.raises(c.HandlerCancelled):
            c.run_cancellable(["sh", "-c", script])
    time.sleep(0.5)
    grandchild = None
    try:
        grandchild = int(open(marker).read().strip())
    except Exception:
        pytest.skip("could not capture grandchild pid")
    finally:
        try:
            os.unlink(marker)
        except OSError:
            pass
    alive = subprocess.run(["kill", "-0", str(grandchild)], capture_output=True).returncode == 0
    if alive:
        os.kill(grandchild, 9)
    assert not alive, "grandchild survived — the process GROUP was not signalled"


def test_without_a_cancellation_source_it_just_runs(fast_poll):
    """CLI and tool callers had no cancellation before this existed."""
    r = c.run_cancellable(["true"])
    assert r.returncode == 0


def test_a_failing_command_still_raises_CalledProcessError(fast_poll):
    with c.cancellable(lambda: None):
        with pytest.raises(subprocess.CalledProcessError):
            c.run_cancellable(["false"])


def test_already_cancelled_never_starts_the_child(fast_poll):
    with c.cancellable(lambda: "terminated"):
        with pytest.raises(c.HandlerCancelled):
            c.run_cancellable(["sh", "-c", "echo should-not-run > /tmp/fw_never"])
    assert not os.path.exists("/tmp/fw_never")


def test_predicate_is_thread_local(fast_poll):
    """A runner executes several tasks concurrently; one must not cancel another."""
    seen = {}

    def worker():
        seen["other"] = c.cancelled_reason()

    with c.cancellable(lambda: "cancel-me"):
        assert c.cancelled_reason() == "cancel-me"
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert seen["other"] is None, "cancellation leaked across threads"


def test_a_broken_predicate_does_not_abort_real_work(fast_poll):
    def boom():
        raise RuntimeError("store down")

    with c.cancellable(boom):
        assert c.cancelled_reason() is None
        c.raise_if_cancelled()          # must not raise
