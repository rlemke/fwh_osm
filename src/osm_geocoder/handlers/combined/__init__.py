"""Combined single-pass OSM extractor.

Runs multiple extraction plugins in a single ``apply_file()`` call,
reducing I/O by N× compared to running each extractor separately.
"""

from .combined_handler import PLUGIN_REGISTRY, CombinedScanResult, combined_scan

__all__ = ["combined_scan", "CombinedScanResult", "PLUGIN_REGISTRY"]
