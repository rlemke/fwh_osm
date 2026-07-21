"""Offline tests for the osm.emergency compute core (no network, no storage)."""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.emergency import emergency_ops as ops


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _pt(lon, lat, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props}




# ---------------------------------------------------------------------------
# Pure logic tier: the implementations live as inline scripts in
# osm_emergency.ffl (script-environments.md). These tests execute THE
# ACTUAL FFL SCRIPT CODE through the runtime's sandboxed ScriptExecutor —
# one source of truth, real coverage.
# ---------------------------------------------------------------------------
import pathlib

_FFL = pathlib.Path(__file__).resolve().parents[1] / "ffl" / "osm_emergency.ffl"


def _script(facet_name: str) -> str:
    import json as _json

    from facetwork import parse as _parse
    from facetwork.emitter import JSONEmitter as _Emitter

    program = _json.loads(_Emitter(include_locations=False).emit(_parse(_FFL.read_text())))
    ns = next(d for d in program["declarations"] if d.get("name") == "osm.emergency")
    decl = next(d for d in ns["declarations"] if d.get("name") == facet_name)
    return decl["pre_script"]["code"]


def _run_script(facet_name: str, params: dict) -> dict:
    from facetwork.runtime.script_executor import ScriptExecutor

    r = ScriptExecutor().execute(_script(facet_name), params)
    assert r.success, r.error
    return r.result


def _cat_blob(name, nearest):
    return json.dumps({"category": name, "nearest_network_km": nearest})


@pytest.fixture()
def read_json(monkeypatch):
    """Route _read_json to an in-memory dict keyed by path."""
    store = {}
    monkeypatch.setattr(ops, "_read_json", lambda p: store[p])
    return store


def test_top_cities_sorts_filters_and_counts_untagged(read_json):
    read_json["pop"] = _fc([
        _pt(4.9, 52.4, name="Amsterdam", population="821752", place="city"),
        _pt(4.5, 51.9, name="Rotterdam", population=623652, place="city"),
        _pt(5.1, 52.1, name="NoPop", place="town"),
        _pt(4.3, 52.1, name="Tiny", population=100, place="town"),
    ])
    r = ops.top_cities("pop", max_cities=10, min_population=25000)
    assert [c["name"] for c in r["cities"]] == ["Amsterdam", "Rotterdam"]
    assert r["city_count"] == 2 and r["untagged_count"] == 1


def test_top_cities_excludes_admin_places_and_dedupes(read_json):
    read_json["pop"] = _fc([
        # admin place node with the whole state's population — must NOT rank
        _pt(-83.4, 32.6, name="Georgia", population=10711908, place="state"),
        _pt(-84.39, 33.75, name="Atlanta", population=498715, place="city"),
        # duplicate node for the same city ~2 km away, lower population
        _pt(-84.41, 33.76, name="Atlanta (dup)", population=400000, place="city"),
        _pt(-81.1, 32.08, name="Savannah", population=147780, place="city"),
        # missing place prop -> excluded as non-settlement
        _pt(-83.63, 32.84, name="Macon", population=157346),
    ])
    r = ops.top_cities("pop", max_cities=10, min_population=25000)
    assert [c["name"] for c in r["cities"]] == ["Atlanta", "Savannah"]
    assert r["excluded_non_city"] == 2  # the state node + place-less Macon


def test_build_category_set_drops_empty_paths_and_zero_feature_layers(read_json):
    read_json["h.geojson"] = _fc([_pt(0, 0)])
    read_json["f.geojson"] = _fc([])  # zero features -> dropped
    read_json["s.geojson"] = _fc([_pt(1, 1)])
    r = ops.build_category_set("h.geojson", "f.geojson", "", "s.geojson")
    assert [c["name"] for c in r["categories"]] == ["hospitals", "shelters"]


def test_build_category_set_fails_loud_when_all_empty():
    with pytest.raises(RuntimeError, match="every facility layer path is empty"):
        ops.build_category_set("", "", "", "")


def test_nearest_candidates_pairs_and_buckets(read_json):
    # facilities at ~0, ~22, ~67 km east of the origin (1 deg lon at lat 0 ~ 111 km)
    read_json["fac"] = _fc([_pt(0.001, 0.0), _pt(0.2, 0.0), _pt(0.6, 0.0)])
    r = ops.nearest_candidates("fac", from_lat=0.0, from_lon=0.0, k=2)
    assert r["facility_count"] == 3 and len(r["pairs"]) == 2
    assert r["pairs"][0]["to_lon"] == 0.001  # nearest first
    assert r["bucket_counts"] == {"10": 1, "25": 2, "50": 2}


def test_category_metrics_script():
    m = json.loads(_run_script("CategoryMetrics", {
        "category": "hospitals", "network_distances": [12.0, 3.5, 8.0],
        "bucket_counts": {"10": 2}, "facility_count": 7,
        "city": {"name": "X", "population": 350000}})["metrics_json"])
    assert m["nearest_network_km"] == 3.5 and m["median_network_km"] == 8.0
    assert m["per_100k"] == 2.0


def test_city_readiness_script_components():
    cr = json.loads(_run_script("CityReadiness", {
        "city": {"name": "A"},
        "category_metrics": [_cat_blob("hospitals", 0.0), _cat_blob("fire", 25.0),
                             _cat_blob("police", None), _cat_blob("shelters", 80.0)],
    })["metrics_json"])
    comps = {k: v["component"] for k, v in cr["categories"].items()}
    assert comps == {"hospitals": 100.0, "fire": 50.0, "police": 0.0, "shelters": 0.0}


def test_city_and_region_readiness_scoring(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "resolve_output_dir", lambda c: str(tmp_path / c))
    city_blob = _run_script("CityReadiness", {
        "city": {"name": "A", "lat": 52.0, "lon": 4.9, "population": 100000},
        "category_metrics": [_cat_blob("hospitals", 0.0), _cat_blob("fire", 25.0),
                             _cat_blob("police", 25.0), _cat_blob("shelters", 50.0)],
    })["metrics_json"]
    # components: 100, 50, 50, 0 -> equal weights -> 50
    r = ops.region_readiness("Testland", [city_blob], "")
    assert r["score"] == 50.0
    fc = json.loads((tmp_path / "emergency" / "regions" / "testland.geojson").read_text())
    assert fc["features"][0]["properties"]["score"] == 50.0
    # weights override: all on hospitals -> 100
    r2 = ops.region_readiness("Testland", [city_blob],
                              '{"hospitals": 1, "fire": 0, "police": 0, "shelters": 0}')
    assert r2["score"] == 100.0


def test_rank_regions_script_orders_and_counts():
    mk = lambda region, score: json.dumps({  # noqa: E731
        "region": region, "score": score,
        "cities": [{"city": {"name": region + "-city"}, "score": score}]})
    r = _run_script("RankRegions", {"region_metrics": [mk("B", 40.0), mk("A", 90.0)]})
    assert [x["region"] for x in r["rankings"]] == ["A", "B"]
    assert r["rankings"][0]["rank"] == 1 and r["region_count"] == 2


def test_region_failure_script_classifies_reason():
    r = _run_script("RegionFailure",
                    {"region": "Haiti", "error": "ApproxRoute: empty network at s3://x/haiti@tol25"})
    blob = json.loads(r["metrics_json"])
    assert blob == {"region": "Haiti", "failed": True,
                    "reason": "no routable major-road network in OSM",
                    "error": "ApproxRoute: empty network at s3://x/haiti@tol25"}
    generic = json.loads(_run_script("RegionFailure", {"region": "X", "error": "boom"})["metrics_json"])
    assert generic["reason"] == "analysis failed"


def test_rank_regions_script_excludes_failed_regions():
    mk = lambda region, score: json.dumps({  # noqa: E731
        "region": region, "score": score,
        "cities": [{"city": {"name": region + "-city"}, "score": score}]})
    failed = _run_script("RegionFailure",
                         {"region": "Haiti", "error": "ApproxRoute: empty network"})["metrics_json"]
    r = _run_script("RankRegions", {"region_metrics": [mk("B", 40.0), failed, mk("A", 90.0)]})
    # ranked rows first (sorted), failed rows appended, count = ranked only
    assert [x["region"] for x in r["rankings"]] == ["A", "B", "Haiti"]
    assert r["region_count"] == 2
    assert r["rankings"][-1]["failed"] is True
    assert "rank" not in r["rankings"][-1]


def test_render_atlas_excluded_row(monkeypatch, tmp_path, read_json):
    monkeypatch.setattr(ops, "resolve_output_dir", lambda c: str(tmp_path / c))
    read_json["layer"] = _fc([_pt(4.9, 52.4, name="Amsterdam", region="NL",
                                  population=821752, score=77.5, breakdown="{}")])
    r = ops.render_atlas("layer", [
        {"rank": 1, "region": "NL", "score": 77.5, "best_city": "Amsterdam"},
        {"region": "Haiti", "failed": True,
         "reason": "no routable major-road network in OSM"},
    ], "Test atlas")
    html = (tmp_path / "emergency" / "atlas" / "index.html").read_text()
    assert r["html_path"].endswith("atlas/index.html")
    assert "excluded" in html
    assert "no routable major-road network in OSM" in html
    assert "Haiti" in html


def test_render_atlas_smoke(monkeypatch, tmp_path, read_json):
    monkeypatch.setattr(ops, "resolve_output_dir", lambda c: str(tmp_path / c))
    read_json["layer"] = _fc([_pt(4.9, 52.4, name="Amsterdam", region="NL",
                                  population=821752, score=77.5, breakdown="{}")])
    r = ops.render_atlas("layer", [{"rank": 1, "region": "NL", "score": 77.5,
                                    "best_city": "Amsterdam"}], "Test atlas")
    html = (tmp_path / "emergency" / "atlas" / "index.html").read_text()
    assert r["html_path"].endswith("atlas/index.html")
    assert "Test atlas" in html and "maplibre-gl" in html and "Amsterdam" in html
    assert "thinly mapped" in html  # shelter honesty disclosure present


def test_category_metrics_script_unroutable_sentinel():
    """ApproxRoute's -1.0 unroutable sentinel is excluded from nearest/median
    and flagged as network_unroutable when NO pair routed (Haiti case)."""
    m = json.loads(_run_script("CategoryMetrics", {
        "category": "hospitals", "network_distances": [-1.0, -1.0, -1.0],
        "bucket_counts": {"10": 2}, "facility_count": 3,
        "city": {"name": "PortAuPrince", "population": 1000000}})["metrics_json"])
    assert m["nearest_network_km"] is None
    assert m["median_network_km"] is None
    assert m["network_unroutable"] is True
    assert m["facility_count"] == 3            # crow-flies data still honest
    m2 = json.loads(_run_script("CategoryMetrics", {
        "category": "hospitals", "network_distances": [-1.0, 12.0],
        "bucket_counts": {}, "facility_count": 2, "city": {}})["metrics_json"])
    assert m2["nearest_network_km"] == 12.0
    assert "network_unroutable" not in m2
