#!/usr/bin/env python3
"""OSM Geocoder Agent — handles geocoding and OSM data processing events.

This agent polls for event tasks across all OSM namespaces:
- osm.Geocode: address geocoding via Nominatim API
- osm.cache.*: ~250 region cache handlers (Geofabrik URLs)
- osm.ops.*: download, tile, routing graph operations
- osm.POIs.*: point-of-interest extraction

Usage:
    PYTHONPATH=. python examples/osm-geocoder/agent.py

For Docker/MongoDB mode, set environment variables:
    AFL_MONGODB_URL=mongodb://localhost:27017
    AFL_MONGODB_DATABASE=facetwork

Requires:
    pip install requests
"""

import os

import requests

from facetwork.runtime.agent_runner import AgentConfig, run_agent

config = AgentConfig(service_name="osm-geocoder", server_group="osm")

# Nominatim API (free, no API key required — please respect usage policy)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Facetwork-OSM-Example/1.0"


def geocode_handler(payload: dict) -> dict:
    """Handle osm.Geocode event: resolve address to coordinates.

    Args:
        payload: {"address": "some address string"}

    Returns:
        {"result": {"lat": "...", "lon": "...", "display_name": "..."}}

    Raises:
        ValueError: If the address cannot be resolved
    """
    address = payload.get("address", "")
    step_log = payload.get("_step_log")
    if not address:
        raise ValueError("No address provided")

    response = requests.get(
        NOMINATIM_URL,
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=10,
    )
    response.raise_for_status()

    results = response.json()
    if not results:
        raise ValueError(f"No results found for address: {address}")

    result = results[0]
    if step_log:
        step_log(f"Geocode: '{address}' -> ({result['lat']}, {result['lon']})")
    return {
        "result": {
            "lat": result["lat"],
            "lon": result["lon"],
            "display_name": result["display_name"],
        }
    }


def register(poller=None, runner=None):
    """Register OSM handlers with the active poller or runner."""
    from osm_geocoder.handlers import register_all_handlers, register_all_registry_handlers

    if poller:
        poller.register("osm.Geocode", geocode_handler)
        register_all_handlers(poller)
    if runner:
        runner.register_handler(
            facet_name="osm.Geocode",
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="geocode_handler",
        )
        register_all_registry_handlers(runner)


if __name__ == "__main__":
    run_agent(config, register)
