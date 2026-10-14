"""`_ensure_cities_file` must EXTRACT cities, not fabricate an empty file.

Regression for the 2026-09-22 Washington runs: the helper wrote an empty
FeatureCollection and returned, so `build_anchors` found no city over the
population threshold and silently substituted high-degree graph nodes, and
`detect_bypasses`/`detect_rings` returned {} on the empty file. Every low-zoom
result ever produced had topology-only anchors and empty bypass/ring flags,
while the run reported success and a `city_count` of 50 that was really the
z2 anchor count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_builder  # noqa: E402


class _Scan:
    def __init__(self, results):
        self.results = results


def _places(n: int) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"place": "city", "name": f"C{i}", "population": 100_000 + i},
                "geometry": {"type": "Point", "coordinates": [-122.0 + i * 0.1, 47.0]},
            }
            for i in range(n)
        ],
    }


def test_extracts_places_from_the_pbf(tmp_path, monkeypatch):
    src = tmp_path / "wa_population.geojson"
    src.write_text(json.dumps(_places(3)))
    monkeypatch.setattr(
        zoom_builder, "combined_scan",
        lambda pbf, cats, **kw: _Scan({"population": {"output_path": str(src)}}),
    )
    dest = tmp_path / "out" / "cities.geojson"
    assert zoom_builder._ensure_cities_file("wa.osm.pbf", str(dest), output_dir=str(tmp_path)) == 3
    written = json.loads(dest.read_text())
    assert len(written["features"]) == 3
    assert written["features"][0]["properties"]["population"] == 100_000


def test_scan_is_heartbeat_and_cancel_aware(tmp_path, monkeypatch):
    seen = {}

    def fake_scan(pbf, cats, **kw):
        seen.update(kw)
        return _Scan({"population": {"output_path": ""}})

    monkeypatch.setattr(zoom_builder, "combined_scan", fake_scan)
    beats: list[str] = []
    zoom_builder._ensure_cities_file(
        "wa.osm.pbf", str(tmp_path / "c.geojson"), heartbeat=beats.append, cancel_check=lambda: None
    )
    assert beats and "cities" in beats[0]
    assert seen.get("heartbeat") is not None and seen.get("cancel_check") is not None


def test_an_existing_file_is_reused_and_counted(tmp_path, monkeypatch):
    dest = tmp_path / "cities.geojson"
    dest.write_text(json.dumps(_places(2)))

    def explode(*a, **kw):
        raise AssertionError("must not re-scan when the file exists")

    monkeypatch.setattr(zoom_builder, "combined_scan", explode)
    assert zoom_builder._ensure_cities_file("wa.osm.pbf", str(dest)) == 2


@pytest.mark.parametrize(
    "scan",
    [
        lambda pbf, cats, **kw: _Scan({"population": {"output_path": ""}}),
        lambda pbf, cats, **kw: _Scan({}),
        lambda pbf, cats, **kw: (_ for _ in ()).throw(RuntimeError("osmium exploded")),
    ],
)
def test_failure_to_extract_degrades_and_reports_zero(tmp_path, monkeypatch, scan):
    """Still writes a usable (empty) file so the run continues — but returns 0,
    which is what makes the degradation visible instead of inferred."""
    monkeypatch.setattr(zoom_builder, "combined_scan", scan)
    dest = tmp_path / "cities.geojson"
    assert zoom_builder._ensure_cities_file("wa.osm.pbf", str(dest)) == 0
    assert json.loads(dest.read_text())["features"] == []


def test_city_count_is_cities_not_anchors():
    """`city_count` must not be re-derived from the z2 anchors — that reported
    50 'cities' for a run whose cities file was empty."""
    src = Path(zoom_builder.__file__).read_text()
    assert '"city_count": city_count' in src
    assert '"city_count": len(anchors_by_zoom' not in src


def test_a_cancelled_scan_propagates_and_writes_nothing(tmp_path, monkeypatch):
    """Measured 2026-10-14 (US z2-z4): a rollout recreated the container mid-scan,
    the graceful stop cancelled the handler, and the broad ``except`` turned the
    cancel into "no cities" -- writing an EMPTY file into the deterministic work
    dir. The reclaimed task landed on the same host, reused it, and routed
    nothing: 0 cities, 0 routes, reported as a success."""
    from osm_geocoder.tools._osm_tools.cancellation import HandlerCancelled

    def cancelled(*a, **kw):
        raise HandlerCancelled("task reclaimed")

    monkeypatch.setattr(zoom_builder, "combined_scan", cancelled)
    dest = tmp_path / "cities.geojson"
    with pytest.raises(HandlerCancelled):
        zoom_builder._ensure_cities_file("wa.osm.pbf", str(dest))
    assert not dest.exists()


def test_an_existing_empty_file_is_rescanned(tmp_path, monkeypatch):
    """An empty cities file is a degraded run's leftover, never a result worth
    reusing: a later execution in the same work dir scans again."""
    dest = tmp_path / "cities.geojson"
    dest.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    src = tmp_path / "pop.geojson"
    src.write_text(json.dumps(_places(4)))
    monkeypatch.setattr(
        zoom_builder, "combined_scan",
        lambda pbf, cats, **kw: _Scan({"population": {"output_path": str(src)}}),
    )
    assert zoom_builder._ensure_cities_file("wa.osm.pbf", str(dest)) == 4
