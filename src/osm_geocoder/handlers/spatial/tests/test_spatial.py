"""Deterministic tests for the osm.Spatial distance primitives.

No network, no PBF, no runner — synthetic GeoJSON with known geometry, so the
within/beyond/nearest semantics and the annotated distances are checked against
geodesic ground truth (pyproj.Geod). Reference and subject points sit on the
equator where 0.01° of longitude is a clean ~1.113 km, making the expected
counts obvious.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("shapely")
pytest.importorskip("pyproj")

from pyproj import Geod

from osm_geocoder.handlers.spatial import spatial_ops as ops

_GEOD = Geod(ellps="WGS84")


def _fc(points: list[tuple[float, float, dict]]) -> dict:
    """Build a FeatureCollection of points: (lon, lat, properties)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
            for (lon, lat, props) in points
        ],
    }


def _write(path, fc) -> str:
    path.write_text(json.dumps(fc))
    return str(path)


def _read_features(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)["features"]


def _geodesic_m(lon1, lat1, lon2, lat2) -> float:
    return _GEOD.inv(lon1, lat1, lon2, lat2)[2]


@pytest.fixture
def layers(tmp_path):
    """One reference supermarket at the origin; four subject places east of it."""
    ref = _write(tmp_path / "ref.geojson", _fc([(0.0, 0.0, {"name": "Foodland"})]))
    subjects = _write(
        tmp_path / "subj.geojson",
        _fc(
            [
                (0.00, 0.0, {"id": "A"}),  # ~0 km
                (0.01, 0.0, {"id": "B"}),  # ~1.11 km
                (0.05, 0.0, {"id": "C"}),  # ~5.57 km
                (0.20, 0.0, {"id": "D"}),  # ~22.3 km
            ]
        ),
    )
    return ref, subjects


def test_within_distance_keeps_near_and_annotates(layers, tmp_path):
    ref, subjects = layers
    out = str(tmp_path / "within.geojson")
    res = ops.within_distance(subjects, ref, 10.0, unit="kilometers", output_path=out)

    assert res.operation == "within"
    assert res.original_count == 4
    assert res.reference_count == 1
    feats = _read_features(res.output_path)
    kept_ids = {f["properties"]["id"] for f in feats}
    assert kept_ids == {"A", "B", "C"}  # D (~22 km) is excluded
    assert res.feature_count == 3

    # Distance annotation matches geodesic truth within tolerance.
    by_id = {f["properties"]["id"]: f["properties"] for f in feats}
    expected_b = _geodesic_m(0.0, 0.0, 0.01, 0.0)
    assert by_id["B"]["nearest_distance_m"] == pytest.approx(expected_b, rel=0.01)
    assert by_id["B"]["nearest_distance"] == pytest.approx(expected_b / 1000.0, rel=0.01)
    assert by_id["B"]["nearest_ref_name"] == "Foodland"


def test_beyond_distance_is_the_complement(layers, tmp_path):
    ref, subjects = layers
    out = str(tmp_path / "beyond.geojson")
    res = ops.beyond_distance(subjects, ref, 10.0, unit="kilometers", output_path=out)

    assert res.operation == "beyond"
    feats = _read_features(res.output_path)
    kept_ids = {f["properties"]["id"] for f in feats}
    assert kept_ids == {"D"}  # only the far place is a "desert"
    assert res.feature_count == 1


def test_within_plus_beyond_partition_the_subject(layers, tmp_path):
    ref, subjects = layers
    w = ops.within_distance(subjects, ref, 10.0, unit="kilometers",
                            output_path=str(tmp_path / "w.geojson"))
    b = ops.beyond_distance(subjects, ref, 10.0, unit="kilometers",
                            output_path=str(tmp_path / "b.geojson"))
    # within + beyond exactly partition the subject layer (no overlap, no gap).
    assert w.feature_count + b.feature_count == w.original_count == 4


def test_nearest_keeps_all_and_orders_by_distance(layers, tmp_path):
    ref, subjects = layers
    out = str(tmp_path / "nearest.geojson")
    res = ops.nearest(subjects, ref, unit="kilometers", output_path=out)

    assert res.operation == "nearest"
    feats = _read_features(res.output_path)
    assert res.feature_count == 4  # nearest keeps every subject feature
    dists = {f["properties"]["id"]: f["properties"]["nearest_distance_m"] for f in feats}
    assert dists["A"] < dists["B"] < dists["C"] < dists["D"]
    assert all(f["properties"]["nearest_ref_name"] == "Foodland" for f in feats)


def test_nearest_picks_the_closer_of_two_references(tmp_path):
    # Two supermarkets; a subject between them but closer to the eastern one.
    ref = _write(
        tmp_path / "ref2.geojson",
        _fc([(0.0, 0.0, {"name": "West"}), (0.10, 0.0, {"name": "East"})]),
    )
    subj = _write(tmp_path / "s.geojson", _fc([(0.08, 0.0, {"id": "X"})]))
    res = ops.nearest(subj, ref, unit="kilometers", output_path=str(tmp_path / "n.geojson"))
    props = _read_features(res.output_path)[0]["properties"]
    assert props["nearest_ref_name"] == "East"
    expected = _geodesic_m(0.08, 0.0, 0.10, 0.0)
    assert props["nearest_distance_m"] == pytest.approx(expected, rel=0.01)


def test_empty_reference_layer(tmp_path):
    """An empty reference: nothing is within, everything is beyond."""
    ref = _write(tmp_path / "empty.geojson", _fc([]))
    subj = _write(tmp_path / "s.geojson", _fc([(0.0, 0.0, {"id": "A"}), (1.0, 1.0, {"id": "B"})]))

    within = ops.within_distance(subj, ref, 5.0, unit="kilometers",
                                 output_path=str(tmp_path / "w.geojson"))
    assert within.feature_count == 0
    assert within.reference_count == 0

    beyond = ops.beyond_distance(subj, ref, 5.0, unit="kilometers",
                                 output_path=str(tmp_path / "b.geojson"))
    assert beyond.feature_count == 2  # both kept — beyond an empty reference set


def test_unit_conversion_threshold(layers, tmp_path):
    """1 mile ~= 1.609 km: B (~1.11 km) is within 1 mile, C (~5.57 km) is not."""
    ref, subjects = layers
    res = ops.within_distance(subjects, ref, 1.0, unit="miles",
                              output_path=str(tmp_path / "mi.geojson"))
    kept_ids = {f["properties"]["id"] for f in _read_features(res.output_path)}
    assert kept_ids == {"A", "B"}
    assert res.unit == "miles"
