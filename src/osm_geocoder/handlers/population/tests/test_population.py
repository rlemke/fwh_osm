"""Tests for population filter handlers."""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest
from osm_geocoder.handlers.population.population_filter import (
    HAS_OSMIUM,
    Operator,
    PlaceType,
    PopulationFilteredFeatures,
    PopulationStats,
    calculate_population_stats,
    describe_filter,
    filter_geojson_by_population,
    matches_place_type,
    matches_population,
    parse_population,
)
from osm_geocoder.handlers.population.population_handlers import (
    NAMESPACE,
    POPULATION_FACETS,
    _empty_result,
    _empty_stats,
    _make_filter_by_population_handler,
    _make_filter_by_population_range_handler,
    _make_population_stats_handler,
    _result_to_dict,
    _stats_to_dict,
    register_population_handlers,
)

requires_osmium = pytest.mark.skipif(not HAS_OSMIUM, reason="pyosmium not installed")


@pytest.fixture(autouse=True)
def _isolate_output_base(tmp_path, monkeypatch):
    """Redirect the OSM output base to a tmp dir.

    Handlers resolve their durable output base from FW_DATA_ROOT (when it is a
    remote s3://, hdfs:// store) or FW_OSM_OUTPUT_BASE, falling back to
    FW_OUTPUT_BASE (default ``/Volumes/afl_data/output``). Clear the remote
    overrides and point FW_OUTPUT_BASE at a per-test tmp dir so the suite writes
    locally and never touches the real store — even when run on a fleet host
    whose env has FW_DATA_ROOT=s3://… (otherwise the extractor writes to s3 and
    the test's local os.unlink fails).
    """
    for var in ("FW_DATA_ROOT", "AFL_DATA_ROOT", "FW_OSM_OUTPUT_BASE", "AFL_OSM_OUTPUT_BASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FW_OUTPUT_BASE", str(tmp_path / "output"))


class TestPlaceType:
    """Tests for PlaceType enum."""

    def test_from_string_city(self):
        """Test parsing city variants."""
        assert PlaceType.from_string("city") == PlaceType.CITY
        assert PlaceType.from_string("cities") == PlaceType.CITY
        assert PlaceType.from_string("CITY") == PlaceType.CITY

    def test_from_string_town(self):
        """Test parsing town variants."""
        assert PlaceType.from_string("town") == PlaceType.TOWN
        assert PlaceType.from_string("towns") == PlaceType.TOWN

    def test_from_string_village(self):
        """Test parsing village variants."""
        assert PlaceType.from_string("village") == PlaceType.VILLAGE
        assert PlaceType.from_string("villages") == PlaceType.VILLAGE

    def test_from_string_country(self):
        """Test parsing country variants."""
        assert PlaceType.from_string("country") == PlaceType.COUNTRY
        assert PlaceType.from_string("countries") == PlaceType.COUNTRY
        assert PlaceType.from_string("nation") == PlaceType.COUNTRY

    def test_from_string_state(self):
        """Test parsing state variants."""
        assert PlaceType.from_string("state") == PlaceType.STATE
        assert PlaceType.from_string("states") == PlaceType.STATE
        assert PlaceType.from_string("province") == PlaceType.STATE
        assert PlaceType.from_string("provinces") == PlaceType.STATE

    def test_from_string_county(self):
        """Test parsing county variants."""
        assert PlaceType.from_string("county") == PlaceType.COUNTY
        assert PlaceType.from_string("counties") == PlaceType.COUNTY

    def test_from_string_all(self):
        """Test parsing all/wildcard."""
        assert PlaceType.from_string("all") == PlaceType.ALL
        assert PlaceType.from_string("*") == PlaceType.ALL

    def test_from_string_invalid(self):
        """Test parsing invalid type raises error."""
        with pytest.raises(ValueError, match="Unknown place type"):
            PlaceType.from_string("invalid")

    def test_enum_values(self):
        """Test enum string values."""
        assert PlaceType.CITY.value == "city"
        assert PlaceType.TOWN.value == "town"
        assert PlaceType.VILLAGE.value == "village"
        assert PlaceType.COUNTRY.value == "country"
        assert PlaceType.STATE.value == "state"
        assert PlaceType.COUNTY.value == "county"
        assert PlaceType.ALL.value == "all"


class TestOperator:
    """Tests for Operator enum."""

    def test_from_string_operators(self):
        """Test parsing operators."""
        assert Operator.from_string("gt") == Operator.GT
        assert Operator.from_string(">") == Operator.GT
        assert Operator.from_string("gte") == Operator.GTE
        assert Operator.from_string(">=") == Operator.GTE
        assert Operator.from_string("lt") == Operator.LT
        assert Operator.from_string("<") == Operator.LT
        assert Operator.from_string("lte") == Operator.LTE
        assert Operator.from_string("<=") == Operator.LTE
        assert Operator.from_string("eq") == Operator.EQ
        assert Operator.from_string("=") == Operator.EQ
        assert Operator.from_string("==") == Operator.EQ
        assert Operator.from_string("ne") == Operator.NE
        assert Operator.from_string("!=") == Operator.NE
        assert Operator.from_string("between") == Operator.BETWEEN
        assert Operator.from_string("range") == Operator.BETWEEN

    def test_from_string_invalid(self):
        """Test parsing invalid operator raises error."""
        with pytest.raises(ValueError, match="Unknown operator"):
            Operator.from_string("invalid")


class TestParsePopulation:
    """Tests for population parsing."""

    def test_parse_integer(self):
        """Test parsing integer values."""
        assert parse_population(1234) == 1234
        assert parse_population(0) == 0

    def test_parse_simple_string(self):
        """Test parsing simple string values."""
        assert parse_population("1234") == 1234
        assert parse_population("0") == 0

    def test_parse_with_commas(self):
        """Test parsing values with comma separators."""
        assert parse_population("1,234") == 1234
        assert parse_population("1,234,567") == 1234567

    def test_parse_with_european_format(self):
        """Test parsing European format (period as thousands separator)."""
        assert parse_population("1.234") == 1234
        assert parse_population("1.234.567") == 1234567

    def test_parse_with_approximation(self):
        """Test parsing approximate values."""
        assert parse_population("~1000") == 1000
        assert parse_population("≈1000") == 1000

    def test_parse_with_plus(self):
        """Test parsing values with plus suffix."""
        assert parse_population("1000+") == 1000

    def test_parse_none(self):
        """Test parsing None returns None."""
        assert parse_population(None) is None

    def test_parse_empty_string(self):
        """Test parsing empty string returns None."""
        assert parse_population("") is None
        assert parse_population("   ") is None

    def test_parse_invalid(self):
        """Test parsing invalid values returns None."""
        assert parse_population("unknown") is None
        assert parse_population("N/A") is None


class TestMatchesPlaceType:
    """Tests for place type matching."""

    def test_matches_city(self):
        """Test matching city place type."""
        tags = {"place": "city", "population": "1000000"}
        assert matches_place_type(tags, PlaceType.CITY)
        assert not matches_place_type(tags, PlaceType.TOWN)

    def test_matches_town(self):
        """Test matching town place type."""
        tags = {"place": "town", "population": "50000"}
        assert matches_place_type(tags, PlaceType.TOWN)
        assert not matches_place_type(tags, PlaceType.CITY)

    def test_matches_country_by_admin_level(self):
        """Test matching country by admin_level."""
        tags = {"boundary": "administrative", "admin_level": "2", "population": "1000000"}
        assert matches_place_type(tags, PlaceType.COUNTRY)
        assert not matches_place_type(tags, PlaceType.STATE)

    def test_matches_state_by_admin_level(self):
        """Test matching state by admin_level."""
        tags = {"boundary": "administrative", "admin_level": "4", "population": "5000000"}
        assert matches_place_type(tags, PlaceType.STATE)
        assert not matches_place_type(tags, PlaceType.COUNTY)

    def test_matches_county_by_admin_level(self):
        """Test matching county by admin_level."""
        tags = {"boundary": "administrative", "admin_level": "6", "population": "500000"}
        assert matches_place_type(tags, PlaceType.COUNTY)

    def test_matches_all_requires_population(self):
        """Test ALL type requires population tag."""
        tags_with_pop = {"place": "city", "population": "1000000"}
        tags_without_pop = {"place": "city"}
        assert matches_place_type(tags_with_pop, PlaceType.ALL)
        assert not matches_place_type(tags_without_pop, PlaceType.ALL)


class TestMatchesPopulation:
    """Tests for population threshold matching."""

    def test_greater_than(self):
        """Test greater than operator."""
        assert matches_population(1001, 1000, None, Operator.GT)
        assert not matches_population(1000, 1000, None, Operator.GT)
        assert not matches_population(999, 1000, None, Operator.GT)

    def test_greater_than_or_equal(self):
        """Test greater than or equal operator."""
        assert matches_population(1001, 1000, None, Operator.GTE)
        assert matches_population(1000, 1000, None, Operator.GTE)
        assert not matches_population(999, 1000, None, Operator.GTE)

    def test_less_than(self):
        """Test less than operator."""
        assert matches_population(999, 1000, None, Operator.LT)
        assert not matches_population(1000, 1000, None, Operator.LT)
        assert not matches_population(1001, 1000, None, Operator.LT)

    def test_less_than_or_equal(self):
        """Test less than or equal operator."""
        assert matches_population(999, 1000, None, Operator.LTE)
        assert matches_population(1000, 1000, None, Operator.LTE)
        assert not matches_population(1001, 1000, None, Operator.LTE)

    def test_equal(self):
        """Test equal operator."""
        assert matches_population(1000, 1000, None, Operator.EQ)
        assert not matches_population(1001, 1000, None, Operator.EQ)

    def test_not_equal(self):
        """Test not equal operator."""
        assert matches_population(1001, 1000, None, Operator.NE)
        assert not matches_population(1000, 1000, None, Operator.NE)

    def test_between(self):
        """Test between operator (inclusive)."""
        assert matches_population(1000, 1000, 2000, Operator.BETWEEN)
        assert matches_population(1500, 1000, 2000, Operator.BETWEEN)
        assert matches_population(2000, 1000, 2000, Operator.BETWEEN)
        assert not matches_population(999, 1000, 2000, Operator.BETWEEN)
        assert not matches_population(2001, 1000, 2000, Operator.BETWEEN)

    def test_between_requires_max(self):
        """Test between operator requires max_population."""
        with pytest.raises(ValueError, match="requires max_population"):
            matches_population(1000, 1000, None, Operator.BETWEEN)


class TestDescribeFilter:
    """Tests for filter description generation."""

    def test_describe_gte_filter(self):
        """Test describing GTE filter."""
        desc = describe_filter(PlaceType.CITY, 100000, None, Operator.GTE)
        assert "city" in desc
        assert ">=" in desc
        assert "100,000" in desc

    def test_describe_between_filter(self):
        """Test describing BETWEEN filter."""
        desc = describe_filter(PlaceType.TOWN, 10000, 50000, Operator.BETWEEN)
        assert "town" in desc
        assert "between" in desc
        assert "10,000" in desc
        assert "50,000" in desc

    def test_describe_all_places(self):
        """Test describing filter for all places."""
        desc = describe_filter(PlaceType.ALL, 1000, None, Operator.GTE)
        assert "all places" in desc


class TestFilterGeoJSON:
    """Tests for GeoJSON filtering."""

    def test_filter_by_population(self):
        """Test filtering GeoJSON by population."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"place": "city", "name": "Big City", "population": "1000000"},
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                },
                {
                    "type": "Feature",
                    "properties": {"place": "city", "name": "Small City", "population": "50000"},
                    "geometry": {"type": "Point", "coordinates": [1, 1]},
                },
                {
                    "type": "Feature",
                    "properties": {"place": "town", "name": "Town", "population": "10000"},
                    "geometry": {"type": "Point", "coordinates": [2, 2]},
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            result = filter_geojson_by_population(
                temp_path,
                min_population=100000,
                place_type="city",
                operator="gte",
            )
            assert result.feature_count == 1
            assert result.original_count == 3
            assert result.place_type == "city"

            # Read output and verify
            with open(result.output_path) as f:
                output = json.load(f)
            assert len(output["features"]) == 1
            assert output["features"][0]["properties"]["name"] == "Big City"

            os.unlink(result.output_path)
        finally:
            os.unlink(temp_path)

    def test_filter_by_population_range(self):
        """Test filtering GeoJSON by population range."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"place": "city", "population": "1000000"},
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                },
                {
                    "type": "Feature",
                    "properties": {"place": "city", "population": "500000"},
                    "geometry": {"type": "Point", "coordinates": [1, 1]},
                },
                {
                    "type": "Feature",
                    "properties": {"place": "city", "population": "100000"},
                    "geometry": {"type": "Point", "coordinates": [2, 2]},
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            result = filter_geojson_by_population(
                temp_path,
                min_population=200000,
                max_population=800000,
                place_type="all",
                operator="between",
            )
            assert result.feature_count == 1  # Only 500000 is in range

            os.unlink(result.output_path)
        finally:
            os.unlink(temp_path)


class TestPopulationStats:
    """Tests for population statistics calculation."""

    def test_calculate_stats(self):
        """Test calculating population statistics."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"place": "city", "population": "1000000"},
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                },
                {
                    "type": "Feature",
                    "properties": {"place": "city", "population": "500000"},
                    "geometry": {"type": "Point", "coordinates": [1, 1]},
                },
                {
                    "type": "Feature",
                    "properties": {"place": "city", "population": "100000"},
                    "geometry": {"type": "Point", "coordinates": [2, 2]},
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            stats = calculate_population_stats(temp_path, place_type="all")
            assert stats.total_places == 3
            assert stats.total_population == 1600000
            assert stats.min_population == 100000
            assert stats.max_population == 1000000
            assert stats.avg_population == 533333  # Integer division
        finally:
            os.unlink(temp_path)

    def test_calculate_stats_empty(self):
        """Test calculating stats for empty GeoJSON."""
        geojson = {"type": "FeatureCollection", "features": []}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            stats = calculate_population_stats(temp_path)
            assert stats.total_places == 0
            assert stats.total_population == 0
        finally:
            os.unlink(temp_path)


class TestEmptyResults:
    """Tests for empty result helpers."""

    def test_empty_result(self):
        """Test empty result has correct structure."""
        result = _empty_result("city", 100000, 0)
        assert result["output_path"] == ""
        assert result["feature_count"] == 0
        assert result["original_count"] == 0
        assert result["place_type"] == "city"
        assert result["min_population"] == 100000
        assert result["max_population"] == 0
        assert "extraction_date" in result

    def test_empty_stats(self):
        """Test empty stats has correct structure."""
        stats = _empty_stats()
        assert stats["total_places"] == 0
        assert stats["total_population"] == 0
        assert stats["min_population"] == 0
        assert stats["max_population"] == 0
        assert stats["avg_population"] == 0


class TestResultConversions:
    """Tests for result conversion functions."""

    def test_result_to_dict(self):
        """Test PopulationFilteredFeatures to dict conversion."""
        result = PopulationFilteredFeatures(
            output_path="/tmp/filtered.geojson",
            feature_count=42,
            original_count=100,
            place_type="city",
            min_population=100000,
            max_population=0,
            filter_applied="city with population >= 100,000",
            format="GeoJSON",
            extraction_date="2024-01-15T10:30:00+00:00",
        )
        d = _result_to_dict(result)
        assert d["output_path"] == "/tmp/filtered.geojson"
        assert d["feature_count"] == 42
        assert d["original_count"] == 100
        assert d["place_type"] == "city"

    def test_stats_to_dict(self):
        """Test PopulationStats to dict conversion."""
        stats = PopulationStats(
            total_places=10,
            total_population=5000000,
            min_population=100000,
            max_population=1000000,
            avg_population=500000,
            place_type="city",
        )
        d = _stats_to_dict(stats)
        assert d["total_places"] == 10
        assert d["total_population"] == 5000000


class TestHandlerFactories:
    """Tests for handler factory functions."""

    def test_filter_handler_no_path(self):
        """Test filter handler returns empty when no path."""
        handler = _make_filter_by_population_handler("FilterByPopulation")
        result = handler({})
        assert result["result"]["feature_count"] == 0

    def test_filter_range_handler_no_path(self):
        """Test filter range handler returns empty when no path."""
        handler = _make_filter_by_population_range_handler("FilterByPopulationRange")
        result = handler({})
        assert result["result"]["feature_count"] == 0

    def test_stats_handler_no_path(self):
        """Test stats handler returns empty when no path."""
        handler = _make_population_stats_handler("PopulationStatistics")
        result = handler({})
        assert result["stats"]["total_places"] == 0


class TestHandlerRegistration:
    """Tests for handler registration."""

    def test_population_facets_count(self):
        """Test expected number of population facets."""
        assert len(POPULATION_FACETS) == 5

    def test_population_facets_names(self):
        """Test population facet names."""
        names = [name for name, _ in POPULATION_FACETS]
        assert "FilterByPopulation" in names
        assert "FilterByPopulationRange" in names
        assert "TopNByPopulation" in names
        assert "PopulationStatistics" in names
        assert "AllPopulatedPlaces" in names

    def test_register_population_handlers(self):
        """Test handler registration with mock poller."""
        poller = MagicMock()
        register_population_handlers(poller)

        # Registers one handler per declared facet.
        assert poller.register.call_count == len(POPULATION_FACETS)

        # Verify qualified names are used
        call_args = [call[0][0] for call in poller.register.call_args_list]
        assert f"{NAMESPACE}.FilterByPopulation" in call_args
        assert f"{NAMESPACE}.FilterByPopulationRange" in call_args
        assert f"{NAMESPACE}.TopNByPopulation" in call_args
        assert f"{NAMESPACE}.PopulationStatistics" in call_args
        assert f"{NAMESPACE}.AllPopulatedPlaces" in call_args

    def test_namespace_value(self):
        """Test namespace is correct."""
        assert NAMESPACE == "osm.Population"
