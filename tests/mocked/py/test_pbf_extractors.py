"""Unit tests for the PBF category extractors.

Builds a tiny synthetic ``.osm.pbf`` with ``osmium.SimpleWriter`` (no network,
no MongoDB) and runs each ``extract_*`` primitive on it, asserting feature
counts, geometry types, category/type filtering, and tag preservation. These
cover the Extract layer of the composable facet library
(``docs/architecture/composable-facet-library.md``):

  - ``extract_amenities``               -> Point (nodes + way centroids)
  - ``extract_places_with_population``  -> Point (place nodes)
  - ``extract_parks``                   -> Polygon (area assembly)
  - ``extract_buildings``               -> Polygon (area assembly)

Gated on ``osmium`` + ``shapely`` being importable; skipped otherwise.
"""

import json

import pytest

osmium = pytest.importorskip("osmium")
pytest.importorskip("shapely")

from osm_geocoder.handlers.amenities.amenity_extractor import extract_amenities
from osm_geocoder.handlers.buildings.building_extractor import extract_buildings
from osm_geocoder.handlers.parks.park_extractor import extract_parks
from osm_geocoder.handlers.population.population_filter import extract_places_with_population


@pytest.fixture(scope="module")
def synthetic_pbf(tmp_path_factory):
    """A minimal PBF: amenity/shop/place nodes + one park ring + one building ring."""
    from osmium.osm import mutable

    pbf = str(tmp_path_factory.mktemp("synthpbf") / "synthetic.osm.pbf")

    # Objects must be written in ascending id within each type (nodes, then ways).
    nodes = [
        mutable.Node(
            id=1,
            location=(125.50, -8.50),
            tags={"amenity": "restaurant", "name": "Test Diner", "cuisine": "pizza"},
        ),
        mutable.Node(id=2, location=(125.51, -8.51), tags={"amenity": "cafe", "name": "Test Cafe"}),
        mutable.Node(id=3, location=(125.52, -8.52), tags={"amenity": "bench"}),  # "other"
        mutable.Node(id=4, location=(125.53, -8.53), tags={"shop": "bakery", "name": "Test Bakery"}),
        mutable.Node(
            id=5,
            location=(125.54, -8.54),
            tags={"place": "town", "name": "Testville", "population": "5,000"},
        ),
        mutable.Node(
            id=6, location=(125.55, -8.55), tags={"place": "village", "name": "Smallville"}
        ),  # no population
        # park ring
        mutable.Node(id=10, location=(125.60, -8.60)),
        mutable.Node(id=11, location=(125.61, -8.60)),
        mutable.Node(id=12, location=(125.61, -8.61)),
        mutable.Node(id=13, location=(125.60, -8.61)),
        # building ring
        mutable.Node(id=20, location=(125.70, -8.70)),
        mutable.Node(id=21, location=(125.71, -8.70)),
        mutable.Node(id=22, location=(125.71, -8.71)),
        mutable.Node(id=23, location=(125.70, -8.71)),
    ]
    ways = [
        mutable.Way(id=100, nodes=[10, 11, 12, 13, 10], tags={"leisure": "park", "name": "Central Park"}),
        mutable.Way(
            id=200,
            nodes=[20, 21, 22, 23, 20],
            tags={"building": "house", "name": "Test House", "height": "10"},
        ),
    ]
    writer = osmium.SimpleWriter(pbf)
    try:
        for n in nodes:
            writer.add_node(n)
        for w in ways:
            writer.add_way(w)
    finally:
        writer.close()
    return pbf


def _features(result):
    with open(result.output_path) as f:
        return json.load(f)["features"]


def test_extract_amenities_all(synthetic_pbf, tmp_path):
    out = str(tmp_path / "amenities.geojson")
    r = extract_amenities(synthetic_pbf, output_path=out)
    # restaurant + cafe + bench (other, kept) + shop=bakery
    assert r.feature_count == 4
    feats = _features(r)
    assert {f["geometry"]["type"] for f in feats} == {"Point"}
    # tags preserved, plus derived category + osm_id
    diner = next(f for f in feats if f["properties"].get("name") == "Test Diner")
    assert diner["properties"]["cuisine"] == "pizza"
    assert diner["properties"]["category"] == "food"
    assert diner["properties"]["osm_id"] == 1


def test_extract_amenities_category_filter(synthetic_pbf, tmp_path):
    out = str(tmp_path / "food.geojson")
    r = extract_amenities(synthetic_pbf, category="food", output_path=out)
    names = sorted(f["properties"]["name"] for f in _features(r))
    assert names == ["Test Cafe", "Test Diner"]  # bakery (shopping) + bench (other) excluded
    assert r.amenity_category == "food"


def test_extract_places_population_parsing_and_filter(synthetic_pbf, tmp_path):
    # place_type=town with a >=1000 threshold; "5,000" must parse past the comma
    r = extract_places_with_population(
        synthetic_pbf, place_type="town", min_population=1000, output_path=str(tmp_path / "t.geojson")
    )
    feats = _features(r)
    assert r.feature_count == 1
    assert feats[0]["properties"]["name"] == "Testville"
    assert feats[0]["properties"]["population_value"] == 5000
    assert feats[0]["geometry"]["type"] == "Point"
    assert r.max_population == 5000


def test_extract_places_threshold_excludes(synthetic_pbf, tmp_path):
    r = extract_places_with_population(
        synthetic_pbf, place_type="town", min_population=10000, output_path=str(tmp_path / "x.geojson")
    )
    assert r.feature_count == 0
    assert r.original_count == 1  # Testville was a candidate, filtered out by population


def test_extract_parks_area_assembly(synthetic_pbf, tmp_path):
    r = extract_parks(synthetic_pbf, output_path=str(tmp_path / "parks.geojson"))
    feats = _features(r)
    assert r.feature_count == 1
    assert feats[0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert feats[0]["properties"]["name"] == "Central Park"
    assert feats[0]["properties"]["park_class"]  # derived classification present
    assert r.total_area_km2 > 0


def test_extract_buildings_area_and_height(synthetic_pbf, tmp_path):
    r = extract_buildings(synthetic_pbf, output_path=str(tmp_path / "bld.geojson"))
    feats = _features(r)
    assert r.feature_count == 1
    assert feats[0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert feats[0]["properties"]["building_type"] == "residential"  # house -> residential
    assert r.with_height_data == 1  # carries a height tag
    assert r.total_area_km2 > 0


def test_extract_buildings_type_filter_excludes(synthetic_pbf, tmp_path):
    r = extract_buildings(
        synthetic_pbf, building_type="commercial", output_path=str(tmp_path / "c.geojson")
    )
    assert r.feature_count == 0  # the only building is residential


def test_extract_category_facade_warms_and_slices(synthetic_pbf):
    """The uniform Extract(category) facade returns the right per-category slice.

    A single warm pass extracts the standard category set; this asserts the
    facade routes to the right slice with output_path + feature_count + category.
    (The instant-cache-hit behavior on repeat calls is verified at integration
    scale — a warm category returns in ~0s — not timed here.)
    """
    from osm_geocoder.handlers.sources.pbf_source import _extract_category

    cache = {"path": synthetic_pbf, "region": "synth", "size": 0}

    amenities = _extract_category({"cache": cache, "category": "amenities"})
    assert amenities["category"] == "amenities"
    assert amenities["feature_count"] == 4  # restaurant, cafe, bench, shop=bakery (node-only)
    assert amenities["output_path"]

    # "parks" is in the same warm set, so it is served from the same scan
    parks = _extract_category({"cache": cache, "category": "parks"})
    assert parks["category"] == "parks"
    assert parks["feature_count"] == 1
    assert parks["output_path"]
