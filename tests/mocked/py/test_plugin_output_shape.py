"""`plugin_output` reads a plugin result in EITHER shape.

`CombinedScanResult.results` holds PluginResult dataclasses in-process, but the
same structure arrives as plain dicts once a handler has round-tripped it
through JSON. Callers reached for `.get("output_path")`, which works on the
dict and raises AttributeError on the dataclass. Measured 2026-09-22: that
broke every osm.POIs facet (they re-raise) and silently emptied the low-zoom
builder's city extraction, which reported "0 places" for a PBF holding 2,198.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.combined.plugin_base import PluginResult, plugin_output  # noqa: E402


def test_reads_the_in_process_dataclass():
    r = PluginResult(category="population", output_path="/out/wa_population.geojson",
                     feature_count=2198)
    assert plugin_output(r) == ("/out/wa_population.geojson", 2198)


def test_reads_the_json_round_tripped_dict():
    assert plugin_output({"output_path": "/out/x.geojson", "feature_count": 7}) == \
        ("/out/x.geojson", 7)


@pytest.mark.parametrize("bad", [None, {}, PluginResult(category="c", output_path="", feature_count=0)])
def test_missing_output_is_empty_not_an_exception(bad):
    assert plugin_output(bad) == ("", 0)


def test_no_caller_reaches_for_get_on_a_plugin_result():
    """The two live call sites must go through the accessor."""
    root = Path(__file__).resolve().parents[3] / "src/osm_geocoder/handlers"
    for rel in ("poi/poi_handlers.py", "roads/zoom_builder.py"):
        src = (root / rel).read_text()
        assert "plugin_output(" in src, rel
        assert '.get("output_path")' not in src, rel
