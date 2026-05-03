"""Source adapter handler registration.

Registers all osm.Source.PBF, osm.Source.PostGIS, and osm.Source.GeoJSON
event facet handlers with both AgentPoller and RegistryRunner.
"""

import logging
import os

log = logging.getLogger(__name__)


def register_source_handlers(poller) -> None:
    """Register all source adapter handlers with the poller."""
    from .geojson_source import GEOJSON_DISPATCH
    from .pbf_source import PBF_DISPATCH
    from .postgis_source import POSTGIS_DISPATCH

    for facet_name, handler in PBF_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered PBF source handler: %s", facet_name)

    for facet_name, handler in POSTGIS_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered PostGIS source handler: %s", facet_name)

    for facet_name, handler in GEOJSON_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered GeoJSON source handler: %s", facet_name)


def register_handlers(runner) -> None:
    """Register all source adapter handlers with a RegistryRunner."""
    from .geojson_source import GEOJSON_DISPATCH
    from .pbf_source import PBF_DISPATCH
    from .postgis_source import POSTGIS_DISPATCH

    pbf_uri = f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'pbf_source.py'))}"
    postgis_uri = (
        f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'postgis_source.py'))}"
    )
    geojson_uri = (
        f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'geojson_source.py'))}"
    )

    for facet_name in PBF_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=pbf_uri, entrypoint="handle")

    for facet_name in POSTGIS_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=postgis_uri, entrypoint="handle")

    for facet_name in GEOJSON_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=geojson_uri, entrypoint="handle")
