#!/usr/bin/env python3
"""Unit tests for the Census TIGER voting district handlers.

Run from the repo root:
    pytest examples/osm-geocoder/tests/mocked/py/test_tiger.py -v
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from osm_geocoder.handlers.voting.tiger_downloader import (
    DISTRICT_CONGRESSIONAL,
    DISTRICT_STATE_HOUSE,
    DISTRICT_STATE_SENATE,
    DISTRICT_VOTING_PRECINCT,
    FIPS_TO_STATE,
    STATE_FIPS,
    cache_path,
    resolve_state_fips,
    tiger_url,
)
from osm_geocoder.handlers.voting.tiger_handlers import (
    NAMESPACE_DISTRICTS,
    NAMESPACE_PROCESSING,
    TIGER_FACETS,
    _district_type_name,
    register_tiger_handlers,
)


@pytest.fixture(autouse=True)
def _isolate_output_base(tmp_path, monkeypatch):
    """Redirect the OSM output base to a tmp dir.

    The district-filter handler resolves its output directory from
    ``AFL_OUTPUT_BASE`` (default ``/Volumes/afl_data/output``). Point it at a
    per-test tmp dir so the suite never touches the real data volume.  (The
    ``/Volumes/...`` strings elsewhere in this file are mocked download return
    values, not real writes, and are unaffected.)
    """
    monkeypatch.setenv("AFL_OUTPUT_BASE", str(tmp_path / "output"))


class TestStateFIPSResolution:
    """Tests for resolve_state_fips()."""

    def test_resolve_fips_code(self):
        assert resolve_state_fips("06") == "06"
        assert resolve_state_fips("48") == "48"

    def test_resolve_abbreviation(self):
        assert resolve_state_fips("CA") == "06"
        assert resolve_state_fips("TX") == "48"
        assert resolve_state_fips("ny") == "36"  # Case insensitive

    def test_resolve_full_name(self):
        assert resolve_state_fips("California") == "06"
        assert resolve_state_fips("texas") == "48"
        assert resolve_state_fips("New York") == "36"

    def test_resolve_invalid(self):
        with pytest.raises(ValueError, match="Unknown state"):
            resolve_state_fips("Invalid")
        with pytest.raises(ValueError, match="Unknown FIPS"):
            resolve_state_fips("99")

    def test_all_states_have_fips(self):
        # Verify all 50 states + DC + territories
        assert len(STATE_FIPS) >= 51
        assert "CA" in STATE_FIPS
        assert "DC" in STATE_FIPS
        assert "PR" in STATE_FIPS

    def test_fips_reverse_lookup(self):
        assert FIPS_TO_STATE["06"] == "CA"
        assert FIPS_TO_STATE["48"] == "TX"


class TestTigerURL:
    """Tests for tiger_url()."""

    def test_congressional_url_pre2023(self):
        url = tiger_url(DISTRICT_CONGRESSIONAL, 2020, congress_number=116)
        assert url == "https://www2.census.gov/geo/tiger/TIGER2020/CD/tl_2020_us_cd116.zip"

    def test_congressional_url_2023_per_state(self):
        url = tiger_url(DISTRICT_CONGRESSIONAL, 2023, state_fips="06", congress_number=118)
        assert url == "https://www2.census.gov/geo/tiger/TIGER2023/CD/tl_2023_06_cd118.zip"

    def test_congressional_url_2024_auto_congress(self):
        url = tiger_url(DISTRICT_CONGRESSIONAL, 2024, state_fips="36")
        assert url == "https://www2.census.gov/geo/tiger/TIGER2024/CD/tl_2024_36_cd119.zip"

    def test_congressional_2023_requires_state(self):
        with pytest.raises(ValueError, match="state_fips required"):
            tiger_url(DISTRICT_CONGRESSIONAL, 2023)

    def test_state_senate_url(self):
        url = tiger_url(DISTRICT_STATE_SENATE, 2023, state_fips="06")
        assert url == "https://www2.census.gov/geo/tiger/TIGER2023/SLDU/tl_2023_06_sldu.zip"

    def test_state_house_url(self):
        url = tiger_url(DISTRICT_STATE_HOUSE, 2023, state_fips="48")
        assert url == "https://www2.census.gov/geo/tiger/TIGER2023/SLDL/tl_2023_48_sldl.zip"

    def test_voting_precinct_url_2020(self):
        url = tiger_url(DISTRICT_VOTING_PRECINCT, 2020, state_fips="36")
        assert url == "https://www2.census.gov/geo/tiger/TIGER2020/VTD/tl_2020_36_vtd20.zip"

    def test_voting_precinct_url_2010(self):
        url = tiger_url(DISTRICT_VOTING_PRECINCT, 2010, state_fips="36")
        assert url == "https://www2.census.gov/geo/tiger/TIGER2010/VTD/tl_2010_36_vtd10.zip"

    def test_state_level_requires_fips(self):
        with pytest.raises(ValueError, match="state_fips required"):
            tiger_url(DISTRICT_STATE_SENATE, 2023)
        with pytest.raises(ValueError, match="state_fips required"):
            tiger_url(DISTRICT_STATE_HOUSE, 2023)
        with pytest.raises(ValueError, match="state_fips required"):
            tiger_url(DISTRICT_VOTING_PRECINCT, 2020)

    def test_invalid_district_type(self):
        with pytest.raises(ValueError, match="Unknown district type"):
            tiger_url("invalid", 2023)


class TestCachePath:
    """Tests for cache_path()."""

    def test_congressional_cache_path(self):
        path = cache_path(DISTRICT_CONGRESSIONAL, 2023, congress_number=118)
        assert "2023" in path
        assert "CD" in path
        assert "tl_2023_us_cd118.zip" in path

    def test_state_senate_cache_path(self):
        path = cache_path(DISTRICT_STATE_SENATE, 2023, state_fips="06")
        assert "SLDU" in path
        assert "06" in path
        assert "tl_2023_06_sldu.zip" in path

    def test_state_house_cache_path(self):
        path = cache_path(DISTRICT_STATE_HOUSE, 2023, state_fips="48")
        assert "SLDL" in path
        assert "48" in path

    def test_voting_precinct_cache_path(self):
        path = cache_path(DISTRICT_VOTING_PRECINCT, 2020, state_fips="36")
        assert "VTD" in path
        assert "vtd20" in path


class TestDistrictTypeName:
    """Tests for _district_type_name()."""

    def test_known_types(self):
        assert _district_type_name(DISTRICT_CONGRESSIONAL) == "Congressional Districts"
        assert _district_type_name(DISTRICT_STATE_SENATE) == "State Senate Districts"
        assert _district_type_name(DISTRICT_STATE_HOUSE) == "State House Districts"
        assert _district_type_name(DISTRICT_VOTING_PRECINCT) == "Voting Precincts"

    def test_unknown_type(self):
        assert _district_type_name("unknown") == "unknown"


class TestTigerHandlers:
    """Tests for TIGER event facet handlers."""

    def test_congressional_handler_mock(self):
        """Test Congressional Districts handler with mock download."""
        from osm_geocoder.handlers.voting.tiger_handlers import _make_congressional_handler

        handler = _make_congressional_handler("CongressionalDistricts")

        with patch("osm_geocoder.handlers.voting.tiger_handlers.download_congressional_districts") as mock_dl:
            mock_dl.return_value = {
                "url": "https://example.com/cd.zip",
                "path": "/Volumes/afl_data/output/census/tiger-cache/cd.zip",
                "date": "2024-01-01T00:00:00Z",
                "size": 1000,
                "wasInCache": True,
                "year": 2024,
                "district_type": DISTRICT_CONGRESSIONAL,
                "state": "CA",
            }

            result = handler({"state": "CA", "year": 2024, "congress_number": 119})

            assert result["cache"]["path"] == "/Volumes/afl_data/output/census/tiger-cache/cd.zip"
            assert result["cache"]["year"] == 2024
            mock_dl.assert_called_once_with(2024, 119, state_fips="06")

    def test_state_senate_handler_mock(self):
        """Test State Senate Districts handler with mock download."""
        from osm_geocoder.handlers.voting.tiger_handlers import _make_state_senate_handler

        handler = _make_state_senate_handler("StateSenateDistricts")

        with patch("osm_geocoder.handlers.voting.tiger_handlers.download_state_senate_districts") as mock_dl:
            mock_dl.return_value = {
                "url": "https://example.com/sldu.zip",
                "path": "/Volumes/afl_data/output/census/tiger-cache/sldu.zip",
                "date": "2024-01-01T00:00:00Z",
                "size": 500,
                "wasInCache": False,
                "year": 2024,
                "district_type": DISTRICT_STATE_SENATE,
                "state": "CA",
            }

            result = handler({"state": "CA", "year": 2024})

            assert result["cache"]["state"] == "CA"
            mock_dl.assert_called_once_with("CA", 2024)

    def test_handler_error_returns_empty_cache(self):
        """Test that handler errors return empty cache."""
        from osm_geocoder.handlers.voting.tiger_handlers import _make_voting_precincts_handler

        handler = _make_voting_precincts_handler("VotingPrecincts")

        with patch("osm_geocoder.handlers.voting.tiger_handlers.download_voting_precincts") as mock_dl:
            mock_dl.side_effect = Exception("Network error")

            result = handler({"state": "CA", "year": 2020})

            assert result["cache"]["path"] == ""
            assert result["cache"]["size"] == 0


class TestFilterDistrictsHandler:
    """Tests for the FilterDistricts handler."""

    @pytest.fixture
    def sample_districts_geojson(self, tmp_path):
        """Create a sample GeoJSON file with district features."""
        features = [
            {
                "type": "Feature",
                "properties": {"GEOID": "0601", "NAME": "District 1"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            },
            {
                "type": "Feature",
                "properties": {"GEOID": "0602", "NAME": "District 2"},
                "geometry": {"type": "Polygon", "coordinates": [[[1, 0], [2, 0], [2, 1], [1, 0]]]},
            },
            {
                "type": "Feature",
                "properties": {"GEOID": "0603", "NAME": "District 3"},
                "geometry": {"type": "Polygon", "coordinates": [[[2, 0], [3, 0], [3, 1], [2, 0]]]},
            },
        ]
        geojson = {"type": "FeatureCollection", "features": features}

        path = tmp_path / "districts.geojson"
        with open(path, "w") as f:
            json.dump(geojson, f)
        return path

    def test_filter_by_name(self, sample_districts_geojson, tmp_path):
        """Test filtering districts by attribute."""
        from osm_geocoder.handlers.voting.tiger_handlers import _make_filter_districts_handler

        handler = _make_filter_districts_handler("FilterDistricts")

        result = handler(
            {
                "input_path": str(sample_districts_geojson),
                "attribute": "NAME",
                "value": "District 2",
            }
        )

        assert result["result"]["feature_count"] == 1
        assert os.path.exists(result["result"]["output_path"])

        with open(result["result"]["output_path"]) as f:
            output = json.load(f)
        assert len(output["features"]) == 1
        assert output["features"][0]["properties"]["NAME"] == "District 2"

    def test_filter_no_matches(self, sample_districts_geojson):
        """Test filtering with no matches."""
        from osm_geocoder.handlers.voting.tiger_handlers import _make_filter_districts_handler

        handler = _make_filter_districts_handler("FilterDistricts")

        result = handler(
            {
                "input_path": str(sample_districts_geojson),
                "attribute": "NAME",
                "value": "District 99",
            }
        )

        assert result["result"]["feature_count"] == 0


class TestStateFIPSHandler:
    """Tests for the StateFIPS handler."""

    def test_resolve_california(self):
        from osm_geocoder.handlers.voting.tiger_handlers import _make_state_fips_handler

        handler = _make_state_fips_handler("StateFIPS")

        result = handler({"state": "California"})
        assert result["fips"] == "06"

    def test_resolve_abbreviation(self):
        from osm_geocoder.handlers.voting.tiger_handlers import _make_state_fips_handler

        handler = _make_state_fips_handler("StateFIPS")

        result = handler({"state": "TX"})
        assert result["fips"] == "48"

    def test_resolve_invalid(self):
        from osm_geocoder.handlers.voting.tiger_handlers import _make_state_fips_handler

        handler = _make_state_fips_handler("StateFIPS")

        result = handler({"state": "Invalid"})
        assert result["fips"] == ""


class TestHandlerRegistration:
    """Tests for handler registration."""

    def test_register_tiger_handlers(self):
        """Test that all TIGER handlers are registered."""
        mock_poller = MagicMock()
        register_tiger_handlers(mock_poller)

        registered_names = [call[0][0] for call in mock_poller.register.call_args_list]

        # Check district handlers
        assert f"{NAMESPACE_DISTRICTS}.CongressionalDistricts" in registered_names
        assert f"{NAMESPACE_DISTRICTS}.StateSenateDistricts" in registered_names
        assert f"{NAMESPACE_DISTRICTS}.StateHouseDistricts" in registered_names
        assert f"{NAMESPACE_DISTRICTS}.VotingPrecincts" in registered_names

        # Check processing handlers
        assert f"{NAMESPACE_PROCESSING}.ShapefileToGeoJSON" in registered_names
        assert f"{NAMESPACE_PROCESSING}.FilterDistricts" in registered_names

    def test_facet_count(self):
        """Verify expected number of TIGER facets."""
        assert len(TIGER_FACETS) == 7


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
