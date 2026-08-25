"""BuildAdminSet must signal liveness through its BLOCKING phases.

Regression cover for 2026-08-25: `europe` @ admin_level 2 spent ~30 min in a
single `download_file` call with nothing heartbeating, so the runtime decided
the execution had died and re-dispatched it while it was still running. The task
reached retry_count 2 with TWO concurrent executions on two hosts, each having
re-downloaded the same 40.5 GB extract. No error was ever recorded — the run
just duplicated itself.
"""
import threading
import time

import pytest

from osm_geocoder.handlers.planet import planet_handlers as ph


@pytest.fixture
def fast_interval(monkeypatch):
    monkeypatch.setattr(ph, "_HEARTBEAT_INTERVAL_S", 0.02)


def test_heartbeat_fires_while_a_blocking_call_is_in_progress(fast_interval):
    beats = []
    params = {"_task_heartbeat": lambda **kw: beats.append(kw.get("progress_message", ""))}

    with ph._heartbeating(params, "downloading europe"):
        time.sleep(0.25)                      # stands in for the blocking call
        during = len(beats)

    assert during >= 2, f"expected liveness signals during the block, got {during}"
    assert "downloading europe" in beats[0]


def test_ticker_stops_when_the_block_exits(fast_interval):
    before = threading.active_count()
    params = {"_task_heartbeat": lambda **kw: None}
    with ph._heartbeating(params, "work"):
        time.sleep(0.05)
    time.sleep(0.1)
    assert threading.active_count() <= before, "heartbeat thread outlived its block"


def test_missing_heartbeat_is_a_noop_not_a_crash():
    """Handlers are called directly in tests without runtime-injected keys."""
    with ph._heartbeating({}, "work"):
        pass
    with ph._heartbeating({"_task_heartbeat": None}, "work"):
        pass


def test_a_failing_heartbeat_never_kills_the_work(fast_interval):
    """Liveness reporting is best-effort; losing Mongo must not fail the split."""
    def boom(**kw):
        raise RuntimeError("mongo down")

    done = []
    with ph._heartbeating({"_task_heartbeat": boom}, "work"):
        time.sleep(0.1)
        done.append(True)
    assert done == [True]
