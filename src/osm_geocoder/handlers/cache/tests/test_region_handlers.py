"""Tests for the region cache handlers.

Focused on ``handle_region_download`` (backs ``osm.cache.Download``) since
the resolver handlers are exercised indirectly through the comprehensive
suite in ``test_region_resolver.py``. The download handler does real disk
and HTTP I/O via ``pbf_cache.download_region``, so we patch it here.

Run from this package's repo root:

    pytest src/osm_geocoder/handlers/cache/tests/test_region_handlers.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from osm_geocoder.handlers.cache.region_handlers import (
    _DISPATCH,
    handle,
    handle_cache_region,
    handle_region_download,
)
from osm_geocoder.handlers.shared.region_resolver import resolve_batch


def _empty_region(path: str = "") -> dict:
    return {
        "query": "",
        "name": "",
        "canonical": path,
        "level": "",
        "level_label": "",
        "parent_canonical": "",
        "continent": "",
        "geofabrik_path": path,
    }


def _fake_cache(
    geofabrik_path: str, *, was_in_cache: bool = False, region: dict | None = None
) -> dict:
    """Build an OSMCache-shaped dict for tests, mirroring ``to_osm_cache``.

    Always includes a ``region`` field matching the new OSMCache schema —
    callers can pass a typed Region dict to verify propagation or omit
    it to use a path-only placeholder.
    """
    return {
        "region": region if region is not None else _empty_region(geofabrik_path),
        "url": f"https://download.geofabrik.de/{geofabrik_path}-latest.osm.pbf",
        "path": f"/tmp/cache/{geofabrik_path}.osm.pbf",
        "date": "2024-01-01",
        "size": 12345678,
        "wasInCache": was_in_cache,
        "source": "cache" if was_in_cache else "geofabrik",
    }


@pytest.fixture
def patched_download():
    """Patch the cache library so handlers run without touching disk/net.

    Returns the patched ``download_region`` mock so tests can assert call
    arguments. ``to_osm_cache`` is patched to mirror the real signature
    ``(result, region=region)`` so it can capture the typed Region the
    handler passes in.
    """
    with patch(
        "osm_geocoder.handlers.cache.region_handlers.download_region"
    ) as dl, patch(
        "osm_geocoder.handlers.cache.region_handlers.to_osm_cache",
        side_effect=lambda r, region=None: {**r, "region": region if region is not None else r.get("region", _empty_region())},
    ):
        dl.side_effect = lambda path: _fake_cache(path)
        yield dl


# ---------------------------------------------------------------------------
# handle_region_download
# ---------------------------------------------------------------------------


class TestDownloadHandler:
    def test_uses_geofabrik_path(self, patched_download):
        region = {
            "query": "Quebec",
            "name": "Quebec",
            "canonical": "north-america/canada/quebec",
            "level": "subnational",
            "level_label": "province",
            "parent_canonical": "north-america/canada",
            "continent": "NorthAmerica",
            "geofabrik_path": "north-america/canada/quebec",
        }
        out = handle_region_download({"region": region})
        patched_download.assert_called_once_with("north-america/canada/quebec")
        assert out["cache"]["path"].endswith("quebec.osm.pbf")

    def test_returns_osmcache_shape(self, patched_download):
        region = {
            "geofabrik_path": "africa/algeria",
            "name": "Algeria",
        }
        out = handle_region_download({"region": region})
        assert set(out.keys()) == {"cache"}
        cache = out["cache"]
        # Required OSMCache fields per the FFL schema.
        for field in ("region", "url", "path", "date", "size", "wasInCache"):
            assert field in cache, field

    def test_region_propagated_into_cache(self, patched_download):
        """A typed Region passed in to Download is preserved in OSMCache.region."""
        region = {
            "query": "Quebec",
            "name": "Quebec",
            "canonical": "north-america/canada/quebec",
            "level": "subnational",
            "level_label": "province",
            "parent_canonical": "north-america/canada",
            "continent": "NorthAmerica",
            "geofabrik_path": "north-america/canada/quebec",
        }
        out = handle_region_download({"region": region})
        cached_region = out["cache"]["region"]
        assert cached_region["name"] == "Quebec"
        assert cached_region["level"] == "subnational"
        assert cached_region["level_label"] == "province"
        assert cached_region["continent"] == "NorthAmerica"

    def test_falls_back_to_canonical_when_geofabrik_path_missing(
        self, patched_download
    ):
        """If a hand-constructed Region omits geofabrik_path but has canonical,
        the handler uses canonical as the download path (they are equal in
        the current registry; this guards against caller-side mistakes)."""
        region = {
            "name": "California",
            "canonical": "north-america/us/california",
            # geofabrik_path intentionally absent
        }
        out = handle_region_download({"region": region})
        patched_download.assert_called_once_with("north-america/us/california")
        assert out["cache"]["path"].endswith("california.osm.pbf")

    def test_missing_path_raises(self, patched_download):
        region = {"name": "Mystery", "level": "subnational"}
        with pytest.raises(ValueError, match="geofabrik_path"):
            handle_region_download({"region": region})
        patched_download.assert_not_called()

    def test_empty_path_raises(self, patched_download):
        region = {"geofabrik_path": "", "canonical": "", "name": "Empty"}
        with pytest.raises(ValueError, match="geofabrik_path"):
            handle_region_download({"region": region})

    def test_non_dict_region_raises(self, patched_download):
        with pytest.raises(ValueError, match="must be a Region dict"):
            handle_region_download({"region": "California"})
        with pytest.raises(ValueError, match="must be a Region dict"):
            handle_region_download({"region": ["California"]})

    def test_none_region_raises_missing_path(self, patched_download):
        # None coerces to {} via the `or {}` default; the missing-path branch
        # surfaces the more actionable error message in that case.
        with pytest.raises(ValueError, match="geofabrik_path"):
            handle_region_download({"region": None})

    def test_missing_region_param_raises(self, patched_download):
        # No "region" key at all — defaults to {} which has no path.
        with pytest.raises(ValueError, match="geofabrik_path"):
            handle_region_download({})

    def test_step_log_called_when_provided(self, patched_download):
        logs = []
        region = {
            "name": "Quebec",
            "geofabrik_path": "north-america/canada/quebec",
        }
        handle_region_download({"region": region, "_step_log": logs.append})
        assert len(logs) == 1
        assert "Quebec" in logs[0]
        assert "north-america/canada/quebec" in logs[0]

    def test_was_in_cache_propagated(self):
        """When pbf_cache reports a cache hit, the result reflects it."""
        with patch(
            "osm_geocoder.handlers.cache.region_handlers.download_region",
            return_value=_fake_cache("africa/algeria", was_in_cache=True),
        ), patch(
            "osm_geocoder.handlers.cache.region_handlers.to_osm_cache",
            side_effect=lambda r, region=None: {
                **r,
                "region": region if region is not None else r.get("region", _empty_region()),
            },
        ):
            region = {"geofabrik_path": "africa/algeria", "name": "Algeria"}
            out = handle_region_download({"region": region})
            assert out["cache"]["wasInCache"] is True


# ---------------------------------------------------------------------------
# Dispatch table integration
# ---------------------------------------------------------------------------


class TestDispatchWiring:
    def test_download_registered(self):
        assert "osm.cache.Download" in _DISPATCH
        assert _DISPATCH["osm.cache.Download"] is handle_region_download

    def test_dispatch_through_handle(self, patched_download):
        region = {
            "geofabrik_path": "europe/france",
            "name": "France",
        }
        out = handle(
            {"_facet_name": "osm.cache.Download", "region": region}
        )
        assert out["cache"]["path"].endswith("france.osm.pbf")

    def test_unknown_facet_raises(self):
        with pytest.raises(ValueError, match="Unknown facet"):
            handle({"_facet_name": "osm.cache.NoSuchFacet"})


# ---------------------------------------------------------------------------
# End-to-end: ResolveRegions → foreach → Download
# ---------------------------------------------------------------------------


class TestResolveDownloadFlow:
    """Walks the user's exact target pattern with mocked downloads.

    Verifies the typed Region produced by ``resolve_batch`` can be handed
    directly to ``handle_region_download`` with no field translation —
    the FFL ``foreach r in resolved.regions { Download(region = $.r) }``
    flow shouldn't need any glue code.
    """

    def test_africa_california_europe_quebec(self, patched_download):
        names = ["Africa", "California", "Europe", "Quebec"]
        resolved = resolve_batch(names)

        assert len(resolved.regions) == 4
        assert not resolved.diagnostics.unresolved
        assert not resolved.diagnostics.ambiguous

        # Each typed Region flows through the Download handler as-is.
        for region in resolved.regions:
            out = handle_region_download({"region": region.to_dict()})
            assert "cache" in out
            assert out["cache"]["path"].endswith(".osm.pbf")

        # And the handler invocations matched the canonical paths we expect —
        # one continent download for Africa + Europe, plus the two subnational
        # extracts.
        called_paths = [c.args[0] for c in patched_download.call_args_list]
        assert "africa" in called_paths
        assert "europe" in called_paths
        assert "north-america/us/california" in called_paths
        assert "north-america/canada/quebec" in called_paths
        assert len(called_paths) == 4

    def test_feature_expansion_downloads_all_constituents(
        self, patched_download
    ):
        """A feature like 'Alps' expands to 7 countries — each gets one
        Download call. No deduplication surprises."""
        resolved = resolve_batch(["Alps"])
        assert len(resolved.regions) == 7

        for region in resolved.regions:
            handle_region_download({"region": region.to_dict()})

        called_paths = [c.args[0] for c in patched_download.call_args_list]
        assert len(called_paths) == 7
        assert all(p.startswith("europe/") for p in called_paths)

    def test_typed_region_survives_into_cache(self, patched_download):
        """End-to-end: resolve_batch produces typed Regions whose name +
        level land intact on OSMCache.region — that's the whole point of
        embedding Region in OSMCache (downstream handlers can log
        'Quebec (province)' without re-resolving)."""
        resolved = resolve_batch(["California", "Quebec"])
        outputs = [
            handle_region_download({"region": r.to_dict()})
            for r in resolved.regions
        ]
        by_name = {
            out["cache"]["region"]["name"]: out["cache"]["region"]
            for out in outputs
        }
        assert by_name["California"]["level"] == "subnational"
        assert by_name["California"]["level_label"] == "state"
        assert by_name["Quebec"]["level_label"] == "province"
        assert by_name["California"]["continent"] == "NorthAmerica"


# ---------------------------------------------------------------------------
# handle_cache_region (legacy string-input entry point)
# ---------------------------------------------------------------------------


class TestCacheRegionHandler:
    """The legacy ``osm.ops.CacheRegion(region: String)`` entry point now
    populates OSMCache.region by resolving its string input through
    ``region_from_path``, so downstream handlers don't have to care which
    entry point produced the cache."""

    def test_path_input_populates_region(self, patched_download):
        out = handle_cache_region({"region": "north-america/us/california"})
        cached_region = out["cache"]["region"]
        assert cached_region["name"] == "California"
        assert cached_region["level"] == "subnational"
        assert cached_region["level_label"] == "state"
        assert cached_region["canonical"] == "north-america/us/california"
        assert cached_region["continent"] == "NorthAmerica"

    def test_friendly_name_input_populates_region(self, patched_download):
        # "California" with no slash → handler resolves it to a path,
        # then region_from_path produces the Region.
        out = handle_cache_region({"region": "California"})
        cached_region = out["cache"]["region"]
        assert cached_region["name"] == "California"
        assert cached_region["level"] == "subnational"
        # The user's original query is preserved.
        assert cached_region["query"] == "California"

    def test_continent_path_input(self, patched_download):
        out = handle_cache_region({"region": "africa"})
        cached_region = out["cache"]["region"]
        assert cached_region["level"] == "continent"
        assert cached_region["continent"] == ""  # continents have no parent continent
        assert cached_region["canonical"] == "africa"
