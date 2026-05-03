"""Routing adapter handler registration.

Registers all osm.Routing.API and osm.Routing.OSRM event facet handlers
with both AgentPoller and RegistryRunner.
"""

import logging
import os

log = logging.getLogger(__name__)


def register_routing_adapter_handlers(poller) -> None:
    """Register all routing adapter handlers with the poller."""
    from .api_router import API_DISPATCH
    from .osrm_router import OSRM_DISPATCH

    for facet_name, handler in API_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered API routing handler: %s", facet_name)

    for facet_name, handler in OSRM_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered OSRM routing handler: %s", facet_name)


def register_handlers(runner) -> None:
    """Register all routing adapter handlers with a RegistryRunner."""
    from .api_router import API_DISPATCH
    from .osrm_router import OSRM_DISPATCH

    api_uri = f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'api_router.py'))}"
    osrm_uri = f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'osrm_router.py'))}"

    for facet_name in API_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=api_uri, entrypoint="handle")

    for facet_name in OSRM_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=osrm_uri, entrypoint="handle")
