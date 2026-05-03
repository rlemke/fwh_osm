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
