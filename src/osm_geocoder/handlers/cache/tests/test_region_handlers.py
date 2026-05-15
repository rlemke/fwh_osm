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
    handle_region_download,
)
from osm_geocoder.handlers.shared.region_resolver import resolve_batch


def _fake_cache(geofabrik_path: str, *, was_in_cache: bool = False) -> dict:
    """Build an OSMCache-shaped dict for tests, mirroring ``to_osm_cache``."""
    return {
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
    arguments. The companion ``to_osm_cache`` is patched to a pass-through
    that returns whatever ``download_region`` returned.
    """
    with patch(
        "osm_geocoder.handlers.cache.region_handlers.download_region"
    ) as dl, patch(
        "osm_geocoder.handlers.cache.region_handlers.to_osm_cache",
        side_effect=lambda r: r,
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
        for field in ("url", "path", "date", "size", "wasInCache"):
            assert field in cache, field

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
            side_effect=lambda r: r,
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
