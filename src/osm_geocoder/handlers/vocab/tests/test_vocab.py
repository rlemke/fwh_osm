"""Deterministic tests for the OSM tag vocabulary (osm.Vocab)."""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.vocab import vocab_handlers as H

# Reference the same vocab module the handler uses (via the shim).
vocab = H.vocab_tool


# --- the ontology / resolver ---------------------------------------------------


@pytest.mark.parametrize("term,key,value", [
    ("pharmacy", "amenity", "pharmacy"),
    ("drugstore", "amenity", "pharmacy"),
    ("gas station", "amenity", "fuel"),
    ("petrol station", "amenity", "fuel"),
    ("grocery store", "shop", "supermarket"),
    ("coffee shop", "amenity", "cafe"),
    ("freeway", "highway", "motorway"),
    ("interstate", "highway", "motorway"),
    ("ev charger", "amenity", "charging_station"),
    ("gym", "leisure", "fitness_centre"),
])
def test_resolve_term_to_tag(term, key, value):
    matches = vocab.resolve(term)
    assert matches, f"{term!r} resolved to nothing"
    top = matches[0]
    assert (top.key, top.value) == (key, value)
    assert top.confidence >= 0.9   # exact value or synonym


def test_exact_value_outranks_synonym_confidence():
    assert vocab.resolve("pharmacy")[0].confidence == 1.0     # canonical value
    assert vocab.resolve("drugstore")[0].confidence == 0.9    # synonym


def test_key_filter_constrains():
    assert vocab.resolve("hospital", key="amenity")[0].value == "hospital"
    # "hospital" is not a shop value -> no match under that key
    assert vocab.resolve("hospital", key="shop") == []


def test_unknown_term_resolves_to_nothing():
    assert vocab.resolve("zxqwv nonsense") == []


def test_list_values_and_keys():
    amenities = vocab.list_values("amenity")
    assert {"pharmacy", "fuel", "cafe", "hospital"} <= set(amenities)
    assert {"amenity", "shop", "highway", "leisure"} <= set(vocab.keys())


# --- handler layer -------------------------------------------------------------


def test_resolve_handler_marshals_best_and_alternatives():
    rv = H.handle({"_facet_name": "osm.Vocab.ResolveTag", "term": "grocery store"})["result"]
    assert rv["osm_key"] == "shop"
    assert rv["osm_value"] == "supermarket"
    assert rv["confidence"] >= 0.9
    # alternatives is a JSON string (CombinedScan convention) -> parseable list
    assert isinstance(json.loads(rv["alternatives"]), list)


def test_resolve_handler_unknown_is_confidence_zero():
    rv = H.handle({"_facet_name": "osm.Vocab.ResolveTag", "term": "zxqwv"})["result"]
    assert rv["osm_key"] == "" and rv["osm_value"] == "" and rv["confidence"] == 0.0


def test_resolve_handler_requires_term():
    with pytest.raises(ValueError, match="term is required"):
        H.handle({"_facet_name": "osm.Vocab.ResolveTag", "term": "  "})


def test_list_values_handler():
    rv = H.handle({"_facet_name": "osm.Vocab.ListTagValues", "key": "shop"})
    values = json.loads(rv["values"])
    assert "supermarket" in values
    assert rv["count"] == len(values) > 0


def test_list_values_handler_requires_key():
    with pytest.raises(ValueError, match="key is required"):
        H.handle({"_facet_name": "osm.Vocab.ListTagValues", "key": ""})
