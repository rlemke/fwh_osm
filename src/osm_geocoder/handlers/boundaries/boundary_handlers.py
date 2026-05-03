"""Boundary event facet handlers for OSM boundary extraction.

Handles administrative and natural boundary extraction events defined
in osmboundaries.afl under osm.Boundaries.

All extraction-based handlers have been removed. This module is retained
for structural compatibility with the handler registration system.
"""

import logging

log = logging.getLogger(__name__)

NAMESPACE = "osm.Boundaries"


def register_boundary_handlers(poller) -> None:
    """Register all boundary event facet handlers with the poller."""
    pass


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    raise ValueError(f"Unknown facet: {facet_name}")


def register_handlers(runner) -> None:
    """Register all facets with a RegistryRunner."""
    pass
