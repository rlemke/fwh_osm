"""Event-facet handlers for the ``osm.Vocab`` namespace.

Wires ResolveTag / ListTagValues (osmvocab.ffl) to the tag ontology in
``_osm_tools.vocab``. Pure in-process lookups (no network, no cache needed) that
turn a natural-language term into the OSM ``(key, value)`` it denotes — the
deterministic NL→tag step that lets a composer build "find a pharmacy" as
``amenity=pharmacy`` → ExtractCategory → FilterGeoJSONByOSMType.

``Json``-typed returns (alternatives, values) are emitted as JSON strings, the
same convention the CombinedScan handler uses for its ``results`` field.
"""

from __future__ import annotations

import json
import logging
import os

from ..shared.pbf_convert import vocab as vocab_tool

log = logging.getLogger(__name__)

NAMESPACE = "osm.Vocab"


def _make_resolve_handler(facet_name: str):
    """Resolve a natural-language term to the best OSM (key, value) + alternatives."""

    def handler(payload: dict) -> dict:
        term = (payload.get("term") or "").strip()
        key = (payload.get("key") or "").strip()
        step_log = payload.get("_step_log")
        if not term:
            raise ValueError("ResolveTag: term is required")

        matches = vocab_tool.resolve(term, key=key)
        best = matches[0] if matches else None
        alternatives = [m.to_dict() for m in matches[1:]]

        if step_log:
            if best:
                step_log(f"{facet_name}: {term!r} -> {best.key}={best.value} "
                         f"(conf {best.confidence:.2f}, +{len(alternatives)} alt)", level="success")
            else:
                step_log(f"{facet_name}: {term!r} -> no known tag", level="warning")

        return {
            "result": {
                "osm_key": best.key if best else "",
                "osm_value": best.value if best else "",
                "confidence": best.confidence if best else 0.0,
                "matched_term": best.matched_term if best else "",
                "alternatives": json.dumps(alternatives),
            }
        }

    return handler


def _make_list_values_handler(facet_name: str):
    """List the known OSM values for a tag key (e.g. all amenity values)."""

    def handler(payload: dict) -> dict:
        key = (payload.get("key") or "").strip()
        step_log = payload.get("_step_log")
        if not key:
            raise ValueError("ListTagValues: key is required")
        values = vocab_tool.list_values(key)
        if step_log:
            step_log(f"{facet_name}: {key} -> {len(values)} values", level="success")
        return {"values": json.dumps(values), "count": len(values)}

    return handler


VOCAB_FACETS = [
    ("ResolveTag", _make_resolve_handler),
    ("ListTagValues", _make_list_values_handler),
]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, factory in VOCAB_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = factory(facet_name)


_build_dispatch()


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all osm.Vocab facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_vocab_handlers(poller) -> None:
    """Register all osm.Vocab event facet handlers with an AgentPoller."""
    for facet_name, factory in VOCAB_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, factory(facet_name))
        log.debug("Registered vocab handler: %s", qualified_name)
