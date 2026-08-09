"""Tests for ScanProgressTracker."""

from unittest.mock import MagicMock

from osm_geocoder.handlers.shared.scan_progress import ScanProgressTracker, get_file_size


class TestScanProgressTracker:
    def test_reports_at_milestones(self):
        """Tracker reports progress at ~1/8 intervals."""
        step_log = MagicMock()
        # 800KB file → ~1,667 elements per milestone (800_000 / (8 * 60))
        tracker = ScanProgressTracker(800_000, step_log, label="Test")

        # Process enough elements to trigger at least one milestone
        for _ in range(50_000):
            tracker.tick("node")

        assert step_log.call_count >= 1
        # Check that milestone messages contain the label and percentage
        msg = step_log.call_args_list[0][0][0]
        assert "Test:" in msg
        assert "%" in msg

    def test_no_reports_without_step_log(self):
        """No errors when step_log is None."""
        tracker = ScanProgressTracker(1_000_000, None, label="Test")
        for _ in range(100_000):
            tracker.tick("node")
        tracker.finish()
        # Should not raise

    def test_finish_reports_completion(self):
        step_log = MagicMock()
        tracker = ScanProgressTracker(100_000, step_log, label="Done")
        for _ in range(1_000):
            tracker.tick("node")
        tracker.finish()

        # Last call should be the finish message
        last_msg = step_log.call_args_list[-1][0][0]
        assert "complete" in last_msg
        assert "elem/s" in last_msg

    def test_tracks_element_types(self):
        step_log = MagicMock()
        tracker = ScanProgressTracker(5_000_000, step_log, label="Types")

        for _ in range(30_000):
            tracker.tick("node")
        for _ in range(10_000):
            tracker.tick("way")
        for _ in range(5_000):
            tracker.tick("area")
        for _ in range(1_000):
            tracker.tick("relation")

        assert tracker._nodes == 30_000
        assert tracker._ways == 10_000
        assert tracker._areas == 5_000
        assert tracker._relations == 1_000
        assert tracker._elements == 46_000

    def test_small_file_still_reports(self):
        """Even very small files get at least a finish report."""
        step_log = MagicMock()
        tracker = ScanProgressTracker(1_000, step_log, label="Tiny")
        for _ in range(100):
            tracker.tick("node")
        tracker.finish()
        assert step_log.call_count >= 1

    def test_zero_file_size(self):
        """Handles zero file size gracefully."""
        step_log = MagicMock()
        tracker = ScanProgressTracker(0, step_log, label="Empty")
        for _ in range(100):
            tracker.tick("node")
        tracker.finish()
        # Should not raise

    def test_max_eight_milestone_reports(self):
        """Should not report more than 8 milestone messages (plus finish)."""
        step_log = MagicMock()
        # Small file so milestones are hit quickly
        tracker = ScanProgressTracker(50_000, step_log, label="Cap")
        for _ in range(100_000):
            tracker.tick("node")
        tracker.finish()
        # At most 8 milestone reports + 1 finish = 9
        assert step_log.call_count <= 9


class TestGetFileSize:
    def test_nonexistent_file(self):
        assert get_file_size("/nonexistent/path/file.pbf") == 0

    def test_real_file(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"x" * 12345)
        assert get_file_size(str(f)) == 12345


class TestScanCancellation:
    """A multi-hour PBF scan must stop when the work is no longer wanted.

    The runner injects ``payload["_cancellation_check"]`` — a callable returning
    a reason string once this execution's result would no longer be accepted
    (operator terminate, a watchdog that already failed the task, or a reclaim
    that made this a zombie). The tracker consults it at the one place every
    scanning handler already goes through: ``tick()``.
    """

    def test_scan_aborts_when_cancelled(self):
        from facetwork.runtime.cancellation import HandlerCancelled

        tracker = ScanProgressTracker(
            1_000_000, None, label="Test", cancel_check=lambda: "task was canceled"
        )
        tracker.CANCEL_CHECK_INTERVAL = 0  # check on every tick for the test

        ticks = 0
        try:
            for _ in range(1000):
                tracker.tick("node")
                ticks += 1
        except HandlerCancelled as exc:
            assert exc.reason == "task was canceled"
        else:
            raise AssertionError("scan ignored cancellation")
        assert ticks < 1000, "scan ran to completion despite being cancelled"

    def test_healthy_scan_is_not_interrupted(self):
        tracker = ScanProgressTracker(1_000_000, None, label="Test", cancel_check=lambda: None)
        tracker.CANCEL_CHECK_INTERVAL = 0
        for _ in range(1000):
            tracker.tick("node")
        tracker.finish()

    def test_cancellation_works_without_step_log(self):
        """The heartbeat path bails when step_log is None — cancellation must not.

        A scan with no logging is still cancellable; wiring the check inside
        _maybe_heartbeat would have silently skipped exactly those runs.
        """
        from facetwork.runtime.cancellation import HandlerCancelled

        tracker = ScanProgressTracker(
            1_000_000, None, label="Test", cancel_check=lambda: "task was reclaimed"
        )
        tracker.CANCEL_CHECK_INTERVAL = 0
        try:
            for _ in range(100):
                tracker.tick("node")
        except HandlerCancelled:
            return
        raise AssertionError("un-logged scan was not cancellable")

    def test_check_is_time_gated_not_per_element(self):
        """tick() runs tens of millions of times — the check must not."""
        calls = []
        tracker = ScanProgressTracker(
            1_000_000, None, label="Test", cancel_check=lambda: calls.append(1)
        )
        for _ in range(100_000):
            tracker.tick("node")
        assert len(calls) <= 2, f"cancellation check ran {len(calls)} times in one scan"

    def test_no_check_injected_is_a_no_op(self):
        """Handlers on an older runner (no injected key) keep working."""
        tracker = ScanProgressTracker(1_000_000, None, label="Test", cancel_check=None)
        for _ in range(10_000):
            tracker.tick("node")
        tracker.finish()
