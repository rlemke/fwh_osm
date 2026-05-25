"""Routing adapter handler registration.

Registers all osm.Routing.{API,OSRM,Valhalla,GraphHopper} event facet handlers
with both AgentPoller and RegistryRunner. The adapters share result schemas
(osm.Routing.Types), so a workflow swaps routing engines by namespace alone.
"""

import logging
import os

log = logging.getLogger(__name__)


def register_routing_adapter_handlers(poller) -> None:
    """Register all routing adapter handlers with the poller."""
    from .api_router import API_DISPATCH
    from .graphhopper_router import GRAPHHOPPER_DISPATCH
    from .osrm_router import OSRM_DISPATCH
    from .pgrouting_router import PGROUTING_DISPATCH
    from .valhalla_router import VALHALLA_DISPATCH

    for label, dispatch in (("API", API_DISPATCH), ("OSRM", OSRM_DISPATCH),
                            ("Valhalla", VALHALLA_DISPATCH), ("GraphHopper", GRAPHHOPPER_DISPATCH),
                            ("PgRouting", PGROUTING_DISPATCH)):
        for facet_name, handler in dispatch.items():
            poller.register(facet_name, handler)
            log.debug("Registered %s routing handler: %s", label, facet_name)


def register_handlers(runner) -> None:
    """Register all routing adapter handlers with a RegistryRunner."""
    from .api_router import API_DISPATCH
    from .graphhopper_router import GRAPHHOPPER_DISPATCH
    from .osrm_router import OSRM_DISPATCH
    from .pgrouting_router import PGROUTING_DISPATCH
    from .valhalla_router import VALHALLA_DISPATCH

    here = os.path.dirname(__file__)

    def _uri(module: str) -> str:
        return f"file://{os.path.abspath(os.path.join(here, module))}"

    for dispatch, module in ((API_DISPATCH, "api_router.py"), (OSRM_DISPATCH, "osrm_router.py"),
                             (VALHALLA_DISPATCH, "valhalla_router.py"),
                             (GRAPHHOPPER_DISPATCH, "graphhopper_router.py"),
                             (PGROUTING_DISPATCH, "pgrouting_router.py")):
        uri = _uri(module)
        for facet_name in dispatch:
            runner.register_handler(facet_name=facet_name, module_uri=uri, entrypoint="handle")
