"""Region-name mapping between extract stores is CONFIG, not code.

Geofabrik's key is canonical in this codebase; a store that spells a region
differently (OSM France and our self-hosted split tree both say `oceania` for
`australia-oceania`) is reconciled through a shipped JSON table so that adding
a store, or following a rename, is a config edit rather than a release.
"""
import json

import pytest

from osm_geocoder.tools._osm_tools import pbf_download as m


@pytest.fixture(autouse=True)
def _clear_cache():
    m._region_aliases.cache_clear()
    yield
    m._region_aliases.cache_clear()


def test_shipped_table_remaps_the_one_region_that_differs():
    assert m.remap_region("australia-oceania", "selfhost") == "oceania"
    assert m.remap_region("australia-oceania", "osmfr") == "oceania"


def test_only_the_continent_segment_is_remapped():
    # Stores agree on country/state names, so deeper segments pass through.
    assert m.remap_region("australia-oceania/fiji", "selfhost") == "oceania/fiji"
    assert m.remap_region("europe/germany", "selfhost") == "europe/germany"


def test_unknown_store_and_unknown_region_are_identity():
    assert m.remap_region("europe", "nosuchstore") == "europe"
    assert m.remap_region("antarctica", "selfhost") == "antarctica"


def test_table_is_overridable_without_touching_code(tmp_path, monkeypatch):
    """The point of the file: a deployment can re-spell regions for its own store."""
    custom = tmp_path / "aliases.json"
    custom.write_text(json.dumps({"selfhost": {"europe": "eu", "australia-oceania": "oz"}}))
    monkeypatch.setattr(m, "_REGION_ALIASES_FILE", str(custom))
    m._region_aliases.cache_clear()

    assert m.remap_region("europe/germany", "selfhost") == "eu/germany"
    assert m.remap_region("australia-oceania", "selfhost") == "oz"


def test_missing_or_malformed_table_degrades_to_identity(tmp_path, monkeypatch):
    """A broken table must not break downloads — identity is the safe default."""
    for bad in (tmp_path / "absent.json", tmp_path / "bad.json"):
        if bad.name == "bad.json":
            bad.write_text("{not json")
        monkeypatch.setattr(m, "_REGION_ALIASES_FILE", str(bad))
        m._region_aliases.cache_clear()
        assert m.remap_region("australia-oceania", "selfhost") == "australia-oceania"


def test_osmfr_path_still_applies_both_rules():
    """Remap the continent AND underscore the rest — unchanged behaviour."""
    assert m._osmfr_region("australia-oceania/new-zealand") == "oceania/new_zealand"
    assert m._osmfr_region("africa/burkina-faso") == "africa/burkina_faso"
