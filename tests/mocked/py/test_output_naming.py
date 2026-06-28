"""Tests for collision-safe, param-addressed output naming (shared/_output.py).

Covers the fix for derived leaf artifacts (filtered GeoJSON, rendered maps)
overwriting each other when two different queries shared the same input stem.
The name now encodes the discriminating parameters; an opt-in per-run directory
isolates outputs by execution id when FW_OUTPUT_PER_RUN is set.
"""

from osm_geocoder.handlers.shared import _output
from osm_geocoder.handlers.shared._output import (
    _slugify_discriminators,
    derive_output_path,
)


def test_slugify_readable_and_drops_wildcards():
    assert _slugify_discriminators(("amenity", "fast_food")) == "amenity-fast_food"
    # "*" (any), None, "" are not discriminating — dropped
    assert _slugify_discriminators(("*", "amenity", "cafe")) == "amenity-cafe"
    assert _slugify_discriminators((None, "")) == ""
    # spaces/odd chars sanitized to "_", and "I " stays distinct from "I"
    assert _slugify_discriminators(("node", "ref", "I ")) == "node-ref-I_"
    assert _slugify_discriminators(("ref", "I")) == "ref-I"


def test_slugify_hash_fallback_is_bounded_deterministic_and_distinct():
    long_a = ("amenity", "x" * 60)
    long_b = ("amenity", "y" * 60)
    sa = _slugify_discriminators(long_a)
    sb = _slugify_discriminators(long_b)
    assert len(sa) <= 32  # bounded (readable head + short hash), not the raw 60+ chars
    assert sa == _slugify_discriminators(long_a)  # deterministic
    assert sa != sb  # distinct inputs -> distinct slug (no collision)
    assert sa.startswith("amenity_")  # keeps a readable head for orientation


def test_derive_output_path_distinct_per_query(monkeypatch, tmp_path):
    monkeypatch.setattr(_output, "resolve_output_dir", lambda c: str(tmp_path / c))
    ff = derive_output_path("osm-filtered", "ca_amenities", "filtered", "*", "amenity", "fast_food")
    cafe = derive_output_path("osm-filtered", "ca_amenities", "filtered", "*", "amenity", "cafe")
    assert ff != cafe  # the collision the fix targets
    assert ff.endswith("ca_amenities_filtered_amenity-fast_food.geojson")
    assert cafe.endswith("ca_amenities_filtered_amenity-cafe.geojson")


def test_derive_output_path_idempotent_for_same_query(monkeypatch, tmp_path):
    monkeypatch.setattr(_output, "resolve_output_dir", lambda c: str(tmp_path / c))
    a = derive_output_path("osm-filtered", "s", "filtered", "amenity", "cafe")
    b = derive_output_path("osm-filtered", "s", "filtered", "amenity", "cafe")
    assert a == b  # same query -> same path (safe overwrite with identical content)


def test_per_run_dir_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(_output, "resolve_output_dir", lambda c: str(tmp_path / c))

    monkeypatch.delenv("FW_OUTPUT_PER_RUN", raising=False)
    shared = derive_output_path("osm-filtered", "s", "filtered", "amenity", "cafe", run_id="WID1")
    assert "/runs/" not in shared  # opt-in OFF -> shared dir (cache-friendly default)

    monkeypatch.setenv("FW_OUTPUT_PER_RUN", "1")
    per_run = derive_output_path("osm-filtered", "s", "filtered", "amenity", "cafe", run_id="WID1")
    assert "/runs/WID1/" in per_run  # opt-in ON -> isolated under the execution id

    # even opted-in, a missing run_id falls back to the shared dir
    no_rid = derive_output_path("osm-filtered", "s", "filtered", "amenity", "cafe", run_id=None)
    assert "/runs/" not in no_rid
