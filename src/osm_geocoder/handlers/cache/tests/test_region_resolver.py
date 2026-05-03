#!/usr/bin/env python3
"""Tests for the region resolver module.

Run from the repo root:

    pytest examples/osm-geocoder/tests/mocked/py/test_region_resolver.py -v
"""

import pytest
from osm_geocoder.handlers.shared.region_resolver import (
    ALIASES,
    GEOGRAPHIC_FEATURES,
    RegionMatch,
    ResolutionResult,
    _normalize,
    list_geographic_features,
    list_regions,
    resolve,
)

# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Colorado") == "colorado"

    def test_strip_whitespace(self):
        assert _normalize("  Colorado  ") == "colorado"

    def test_remove_hyphens(self):
        assert _normalize("Czech-Republic") == "czechrepublic"

    def test_remove_underscores(self):
        assert _normalize("Congo_Brazzaville") == "congobrazzaville"

    def test_remove_spaces(self):
        assert _normalize("New York") == "newyork"

    def test_strip_leading_the(self):
        assert _normalize("the Alps") == "alps"

    def test_strip_leading_the_case_insensitive(self):
        assert _normalize("The Netherlands") == "netherlands"

    def test_empty_string(self):
        assert _normalize("") == ""

    def test_only_whitespace(self):
        assert _normalize("   ") == ""

    def test_combined_normalization(self):
        assert _normalize("  The Czech-Republic  ") == "czechrepublic"


# ---------------------------------------------------------------------------
# RegionMatch
# ---------------------------------------------------------------------------


class TestRegionMatch:
    def test_continent_extraction(self):
        m = RegionMatch(
            namespace="osm.cache.Europe",
            facet_name="France",
            geofabrik_path="europe/france",
        )
        assert m.continent == "Europe"

    def test_qualified_name(self):
        m = RegionMatch(
            namespace="osm.cache.Europe",
            facet_name="France",
            geofabrik_path="europe/france",
        )
        assert m.qualified_name == "osm.cache.Europe.France"

    def test_frozen(self):
        m = RegionMatch(
            namespace="osm.cache.Europe",
            facet_name="France",
            geofabrik_path="europe/france",
        )
        with pytest.raises(AttributeError):
            m.namespace = "other"


# ---------------------------------------------------------------------------
# Direct name resolution
# ---------------------------------------------------------------------------


class TestDirectResolution:
    def test_simple_country(self):
        result = resolve("France")
        assert len(result.matches) >= 1
        assert any(m.geofabrik_path == "europe/france" for m in result.matches)

    def test_us_state(self):
        result = resolve("Colorado")
        assert len(result.matches) >= 1
        assert any(m.geofabrik_path == "north-america/us/colorado" for m in result.matches)

    def test_case_insensitive(self):
        result = resolve("france")
        assert any(m.geofabrik_path == "europe/france" for m in result.matches)

    def test_with_hyphens(self):
        result = resolve("Czech-Republic")
        assert any(m.geofabrik_path == "europe/czech-republic" for m in result.matches)

    def test_with_spaces(self):
        result = resolve("New York")
        assert any(m.geofabrik_path == "north-america/us/new-york" for m in result.matches)

    def test_leading_the(self):
        result = resolve("the Netherlands")
        assert any(m.geofabrik_path == "europe/netherlands" for m in result.matches)

    def test_canadian_province(self):
        result = resolve("British Columbia")
        assert any(
            m.geofabrik_path == "north-america/canada/british-columbia" for m in result.matches
        )

    def test_african_country(self):
        result = resolve("Kenya")
        assert any(m.geofabrik_path == "africa/kenya" for m in result.matches)

    def test_asian_country(self):
        result = resolve("Japan")
        assert any(m.geofabrik_path == "asia/japan" for m in result.matches)

    def test_south_american_country(self):
        result = resolve("Brazil")
        assert any(m.geofabrik_path == "south-america/brazil" for m in result.matches)

    def test_central_american_country(self):
        result = resolve("Panama")
        assert any(m.geofabrik_path == "central-america/panama" for m in result.matches)

    def test_oceania_country(self):
        result = resolve("New Zealand")
        assert any(m.geofabrik_path == "australia-oceania/new-zealand" for m in result.matches)

    def test_query_preserved(self):
        result = resolve("France")
        assert result.query == "France"


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


class TestAliasResolution:
    def test_uk(self):
        result = resolve("UK")
        assert any(m.geofabrik_path == "europe/great-britain" for m in result.matches)

    def test_usa(self):
        result = resolve("USA")
        assert any(m.facet_name == "UnitedStates" for m in result.matches)

    def test_great_britain(self):
        result = resolve("Great Britain")
        assert any(m.geofabrik_path == "europe/great-britain" for m in result.matches)

    def test_czechia(self):
        result = resolve("Czechia")
        assert any(m.geofabrik_path == "europe/czech-republic" for m in result.matches)

    def test_holland(self):
        result = resolve("Holland")
        assert any(m.geofabrik_path == "europe/netherlands" for m in result.matches)

    def test_burma(self):
        result = resolve("Burma")
        assert any(m.geofabrik_path == "asia/myanmar" for m in result.matches)

    def test_oceania(self):
        result = resolve("Oceania")
        assert any(m.geofabrik_path == "australia-oceania" for m in result.matches)

    def test_us_state_postal_code_co(self):
        result = resolve("CO")
        assert any(m.geofabrik_path == "north-america/us/colorado" for m in result.matches)

    def test_us_state_postal_code_ny(self):
        result = resolve("NY")
        assert any(m.geofabrik_path == "north-america/us/new-york" for m in result.matches)

    def test_us_state_postal_code_ca(self):
        result = resolve("CA")
        assert any(m.geofabrik_path == "north-america/us/california" for m in result.matches)

    def test_us_state_postal_code_tx(self):
        result = resolve("TX")
        assert any(m.geofabrik_path == "north-america/us/texas" for m in result.matches)

    def test_canadian_province_bc(self):
        result = resolve("BC")
        assert any(
            m.geofabrik_path == "north-america/canada/british-columbia" for m in result.matches
        )

    def test_canadian_province_qc(self):
        result = resolve("QC")
        assert any(m.geofabrik_path == "north-america/canada/quebec" for m in result.matches)

    def test_canadian_province_on(self):
        result = resolve("ON")
        assert any(m.geofabrik_path == "north-america/canada/ontario" for m in result.matches)

    def test_dc(self):
        result = resolve("DC")
        assert any(
            m.geofabrik_path == "north-america/us/district-of-columbia" for m in result.matches
        )

    def test_washington_dc(self):
        result = resolve("Washington DC")
        assert any(
            m.geofabrik_path == "north-america/us/district-of-columbia" for m in result.matches
        )

    def test_england(self):
        result = resolve("England")
        assert any(m.geofabrik_path == "europe/great-britain" for m in result.matches)


# ---------------------------------------------------------------------------
# Ambiguity and disambiguation
# ---------------------------------------------------------------------------


class TestAmbiguity:
    def test_georgia_is_ambiguous(self):
        result = resolve("Georgia")
        assert result.is_ambiguous or len(result.matches) > 1

    def test_georgia_prefer_us(self):
        result = resolve("Georgia", prefer_continent="UnitedStates")
        assert any(m.geofabrik_path == "north-america/us/georgia" for m in result.matches)
        assert not any(m.geofabrik_path == "europe/georgia" for m in result.matches)

    def test_georgia_prefer_europe(self):
        result = resolve("Georgia", prefer_continent="Europe")
        assert any(m.geofabrik_path == "europe/georgia" for m in result.matches)
        assert not any(m.geofabrik_path == "north-america/us/georgia" for m in result.matches)

    def test_unambiguous_country(self):
        result = resolve("France")
        assert not result.is_ambiguous

    def test_disambiguation_message(self):
        result = resolve("Georgia")
        if result.is_ambiguous:
            assert "Georgia" in result.disambiguation
            assert "prefer_continent" in result.disambiguation


# ---------------------------------------------------------------------------
# Geographic features
# ---------------------------------------------------------------------------


class TestGeographicFeatures:
    def test_alps(self):
        result = resolve("Alps")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "europe/austria" in paths
        assert "europe/switzerland" in paths
        assert "europe/france" in paths
        assert "europe/italy" in paths

    def test_the_alps(self):
        result = resolve("the Alps")
        assert result.is_geographic_feature
        assert len(result.matches) >= 4

    def test_rockies(self):
        result = resolve("Rockies")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "north-america/us/colorado" in paths
        assert "north-america/us/montana" in paths

    def test_scandinavia(self):
        result = resolve("Scandinavia")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "europe/norway" in paths
        assert "europe/sweden" in paths
        assert "europe/denmark" in paths

    def test_benelux(self):
        result = resolve("Benelux")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "europe/belgium" in paths
        assert "europe/netherlands" in paths
        assert "europe/luxembourg" in paths

    def test_new_england(self):
        result = resolve("New England")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "north-america/us/massachusetts" in paths
        assert "north-america/us/vermont" in paths
        assert "north-america/us/connecticut" in paths

    def test_baltics(self):
        result = resolve("Baltics")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "europe/estonia" in paths
        assert "europe/latvia" in paths
        assert "europe/lithuania" in paths

    def test_pyrenees(self):
        result = resolve("Pyrenees")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "europe/france" in paths
        assert "europe/spain" in paths
        assert "europe/andorra" in paths

    def test_geographic_feature_no_duplicates(self):
        result = resolve("Alps")
        paths = [m.geofabrik_path for m in result.matches]
        assert len(paths) == len(set(paths))

    def test_andes(self):
        result = resolve("Andes")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "south-america/argentina" in paths
        assert "south-america/chile" in paths
        assert "south-america/peru" in paths

    def test_pacific_northwest(self):
        result = resolve("Pacific Northwest")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "north-america/us/washington" in paths
        assert "north-america/us/oregon" in paths

    def test_east_africa(self):
        result = resolve("East Africa")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "africa/kenya" in paths
        assert "africa/tanzania" in paths

    def test_north_africa(self):
        result = resolve("North Africa")
        assert result.is_geographic_feature
        paths = {m.geofabrik_path for m in result.matches}
        assert "africa/morocco" in paths
        assert "africa/egypt" in paths


# ---------------------------------------------------------------------------
# List functions
# ---------------------------------------------------------------------------


class TestListRegions:
    def test_list_all(self):
        regions = list_regions()
        assert len(regions) > 100  # >280 regions in registry

    def test_list_by_continent_europe(self):
        regions = list_regions(continent="Europe")
        assert all(r.continent == "Europe" for r in regions)
        assert len(regions) > 20

    def test_list_by_continent_africa(self):
        regions = list_regions(continent="Africa")
        assert all(r.continent == "Africa" for r in regions)
        assert len(regions) > 30

    def test_list_by_continent_united_states(self):
        regions = list_regions(continent="UnitedStates")
        assert all(r.continent == "UnitedStates" for r in regions)
        assert len(regions) >= 50

    def test_list_sorted(self):
        regions = list_regions()
        # Should be sorted by continent then facet name
        for i in range(1, len(regions)):
            key_prev = (regions[i - 1].continent, regions[i - 1].facet_name)
            key_curr = (regions[i].continent, regions[i].facet_name)
            assert key_prev <= key_curr

    def test_list_no_duplicates(self):
        regions = list_regions()
        paths = [r.geofabrik_path for r in regions]
        assert len(paths) == len(set(paths))


class TestListGeographicFeatures:
    def test_returns_dict(self):
        features = list_geographic_features()
        assert isinstance(features, dict)

    def test_contains_alps(self):
        features = list_geographic_features()
        assert "alps" in features

    def test_contains_rockies(self):
        features = list_geographic_features()
        assert "rockies" in features

    def test_feature_count(self):
        features = list_geographic_features()
        assert len(features) >= 20


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string(self):
        result = resolve("")
        assert result.matches == []

    def test_whitespace_only(self):
        result = resolve("   ")
        assert result.matches == []

    def test_nonexistent_region(self):
        result = resolve("Atlantis")
        assert result.matches == []

    def test_partial_match_not_supported(self):
        # We do exact match, not fuzzy
        result = resolve("Fran")
        assert result.matches == []

    def test_continent_match(self):
        result = resolve("Africa")
        assert len(result.matches) >= 1
        assert any(m.geofabrik_path == "africa" for m in result.matches)

    def test_geofabrik_leaf_match(self):
        # "czech-republic" is a Geofabrik path leaf
        result = resolve("czech-republic")
        assert any(m.geofabrik_path == "europe/czech-republic" for m in result.matches)

    def test_resolution_result_defaults(self):
        r = ResolutionResult(matches=[], query="test")
        assert not r.is_ambiguous
        assert not r.is_geographic_feature
        assert r.disambiguation == ""

    def test_all_aliases_resolve(self):
        """Every alias should resolve to at least one match."""
        for alias in ALIASES:
            result = resolve(alias)
            assert len(result.matches) >= 1, f"Alias '{alias}' has no matches"

    def test_all_geographic_feature_names_resolve(self):
        """Every geographic feature should resolve to at least one match."""
        for feature_name in GEOGRAPHIC_FEATURES:
            result = resolve(feature_name)
            assert len(result.matches) >= 1, f"Geographic feature '{feature_name}' has no matches"
            assert result.is_geographic_feature
