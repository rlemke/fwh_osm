"""Progress tracking for PBF file scanning.

Reports progress at 1/8 file-size milestones during pyosmium scans.
Since pyosmium's ``apply_file()`` doesn't expose byte position,
progress is estimated from element counts using a calibration window.
"""

import os
import resource
import time
from collections.abc import Callable


def _fmt_elapsed(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS for compact log display."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _memory_mb() -> float:
    """Return current process RSS in MB."""
    # ru_maxrss is in bytes on Linux, KB on macOS
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname == "Darwin":
        return rss / (1024 * 1024)  # macOS: bytes → MB
    return rss / 1024  # Linux: KB → MB


class ScanProgressTracker:
    """Tracks element processing and reports at 1/8 file-size milestones.

    Usage::

        tracker = ScanProgressTracker(file_size, step_log, label="CombinedScan")

        # In each pyosmium callback:
        tracker.tick("node")   # or "way", "area", "relation"

        # After apply_file() returns:
        tracker.finish()
    """

    NUM_CHECKPOINTS = 8
    CALIBRATION_WINDOW = 25_000  # elements before first estimate

    def __init__(
        self,
        file_size: int,
        step_log: Callable | None = None,
        label: str = "Scan",
    ):
        self._file_size = file_size
        self._step_log = step_log
        self._label = label

        self._elements = 0
        self._nodes = 0
        self._ways = 0
        self._areas = 0
        self._relations = 0

        self._next_checkpoint = 1  # 1..NUM_CHECKPOINTS
        self._estimated_total = 0
        self._t0 = time.monotonic()
        self._current_phase: str = ""  # tracks element type for phase-change logs
        self._phase_t0 = self._t0
        self._last_heartbeat = self._t0

        # Pre-compute milestone interval using PBF heuristic.
        # Compressed PBF averages ~40-80 bytes per element; use 60 as midpoint.
        # This gives roughly NUM_CHECKPOINTS reports for any file.
        self._milestone_interval = max(5_000, file_size // (self.NUM_CHECKPOINTS * 60))

    def tick(self, element_type: str = "node") -> None:
        """Call after processing each element."""
        self._elements += 1
        if element_type == "node":
            self._nodes += 1
        elif element_type == "way":
            self._ways += 1
        elif element_type == "area":
            self._areas += 1
        elif element_type == "relation":
            self._relations += 1

        # Log phase transitions (node → way → area → relation)
        if element_type != self._current_phase:
            self._log_phase_change(element_type)

        # After calibration window, refine the estimate using actual density
        if self._elements == self.CALIBRATION_WINDOW:
            self._recalibrate()

        # Check if we hit the next milestone
        if self._estimated_total > 0:
            threshold = int(self._estimated_total * self._next_checkpoint / self.NUM_CHECKPOINTS)
            if self._elements >= threshold and self._next_checkpoint <= self.NUM_CHECKPOINTS:
                self._report()
                self._next_checkpoint += 1
        elif self._elements % self._milestone_interval == 0:
            # Before calibration, use fixed interval
            if self._next_checkpoint <= self.NUM_CHECKPOINTS:
                self._report()
                self._next_checkpoint += 1

        # Time-based heartbeat: emit a log every 30s even if no milestone hit
        self._maybe_heartbeat()

    def _log_phase_change(self, new_phase: str) -> None:
        """Emit a step log when the element type changes."""
        now = time.monotonic()
        if self._step_log and self._current_phase:
            phase_elapsed = now - self._phase_t0
            phase_labels = {
                "node": "nodes",
                "way": "ways",
                "area": "areas",
                "relation": "relations",
            }
            prev_label = phase_labels.get(self._current_phase, self._current_phase)
            count = getattr(self, f"_{self._current_phase}s", 0)
            mem = _memory_mb()
            self._step_log(
                f"{self._label}: finished {prev_label} ({count:,}) in {_fmt_elapsed(phase_elapsed)}, "
                f"starting {phase_labels.get(new_phase, new_phase)} "
                f"(mem: {mem:,.0f}MB)"
            )
        self._current_phase = new_phase
        self._phase_t0 = now

    HEARTBEAT_INTERVAL = 30.0  # seconds between heartbeat logs

    def _maybe_heartbeat(self) -> None:
        """Emit a periodic heartbeat log so the step never appears stuck."""
        if not self._step_log:
            return
        now = time.monotonic()
        if now - self._last_heartbeat < self.HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        elapsed = now - self._t0
        phase_elapsed = now - self._phase_t0
        phase_labels = {
            "node": "nodes",
            "way": "ways",
            "area": "areas",
            "relation": "relations",
        }
        phase = phase_labels.get(self._current_phase, self._current_phase)
        count = getattr(self, f"_{self._current_phase}s", 0) if self._current_phase else 0
        mem = _memory_mb()
        self._step_log(
            f"{self._label}: processing {phase} — "
            f"{count:,} so far in {_fmt_elapsed(phase_elapsed)} "
            f"(total: {self._elements:,} elements in {_fmt_elapsed(elapsed)}, "
            f"mem: {mem:,.0f}MB)"
        )

    def _recalibrate(self) -> None:
        """Refine total element estimate from observed density."""
        if self._elements == 0:
            return
        # Nodes are densely packed (~20-30 bytes each in PBF), but ways/areas
        # are much larger (~100-500 bytes). The calibration window is typically
        # all nodes, so the observed density is too optimistic. Apply a 10x
        # factor to avoid exhausting all checkpoints during the node pass.
        bytes_per_element = self._file_size / max(1, self._elements * 10)
        self._estimated_total = int(self._file_size / max(1, bytes_per_element))
        # Ensure estimate is at least 5x current count
        self._estimated_total = max(self._estimated_total, self._elements * 5)

    def _report(self) -> None:
        if not self._step_log:
            return
        elapsed = time.monotonic() - self._t0
        pct = min(100, int(self._next_checkpoint / self.NUM_CHECKPOINTS * 100))
        size_mb = self._file_size / (1024 * 1024)

        parts = []
        if self._nodes:
            parts.append(f"{self._nodes:,}N")
        if self._ways:
            parts.append(f"{self._ways:,}W")
        if self._areas:
            parts.append(f"{self._areas:,}A")
        if self._relations:
            parts.append(f"{self._relations:,}R")
        counts = "/".join(parts) if parts else f"{self._elements:,}"

        self._step_log(
            f"{self._label}: ~{pct}% of {size_mb:.1f}MB — "
            f"{self._elements:,} elements ({counts}) in {_fmt_elapsed(elapsed)}"
        )

    def finish(self) -> None:
        """Log final scan completion."""
        if not self._step_log:
            return
        # Log final phase duration
        if self._current_phase:
            phase_elapsed = time.monotonic() - self._phase_t0
            phase_labels = {
                "node": "nodes",
                "way": "ways",
                "area": "areas",
                "relation": "relations",
            }
            prev_label = phase_labels.get(self._current_phase, self._current_phase)
            count = getattr(self, f"_{self._current_phase}s", 0)
            self._step_log(
                f"{self._label}: finished {prev_label} ({count:,}) in {_fmt_elapsed(phase_elapsed)}"
            )
        elapsed = time.monotonic() - self._t0
        size_mb = self._file_size / (1024 * 1024)
        rate = self._elements / elapsed if elapsed > 0 else 0
        mem = _memory_mb()
        self._step_log(
            f"{self._label}: scan complete — {self._elements:,} elements "
            f"({self._nodes:,}N/{self._ways:,}W/{self._areas:,}A/{self._relations:,}R) "
            f"from {size_mb:.1f}MB in {_fmt_elapsed(elapsed)} ({rate:,.0f} elem/s, "
            f"peak mem: {mem:,.0f}MB)",
            level="success",
        )


def get_file_size(path: str) -> int:
    """Get file size in bytes, returning 0 if not found."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
