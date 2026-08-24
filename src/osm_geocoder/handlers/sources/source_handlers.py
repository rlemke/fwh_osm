"""Source adapter handler registration.

Registers all osm.Source.PBF, osm.Source.PostGIS, osm.Source.GeoJSON,
osm.Source.Overture and osm.Source.OhsomePlanet event facet handlers with both
AgentPoller and RegistryRunner.
"""

import logging
import os

log = logging.getLogger(__name__)


def register_source_handlers(poller) -> None:
    """Register all source adapter handlers with the poller."""
    from .geojson_source import GEOJSON_DISPATCH
    from .ohsome_source import OHSOME_DISPATCH
    from .overture_source import OVERTURE_DISPATCH
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

    for facet_name, handler in OVERTURE_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered Overture source handler: %s", facet_name)

    for facet_name, handler in OHSOME_DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered ohsome-planet source handler: %s", facet_name)


def register_handlers(runner) -> None:
    """Register all source adapter handlers with a RegistryRunner."""
    from .geojson_source import GEOJSON_DISPATCH
    from .ohsome_source import OHSOME_DISPATCH
    from .overture_source import OVERTURE_DISPATCH
    from .pbf_source import PBF_DISPATCH
    from .postgis_source import POSTGIS_DISPATCH

    pbf_uri = f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'pbf_source.py'))}"
    postgis_uri = (
        f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'postgis_source.py'))}"
    )
    geojson_uri = (
        f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'geojson_source.py'))}"
    )
    overture_uri = (
        f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'overture_source.py'))}"
    )
    ohsome_uri = (
        f"file://{os.path.abspath(os.path.join(os.path.dirname(__file__), 'ohsome_source.py'))}"
    )

    # PBF source facets are full-region osmium scans (Extract*) that read a
    # whole PBF in a blocking C++ loop with sparse heartbeats. On a large
    # region (continental NA is ~19 GB) they far exceed the default 30 s
    # handler timeout and would time-out -> retry -> dead-letter. Register
    # them with timeout_ms=0 so they fall back to the runner's global
    # execution timeout, exactly like osm.Population.AllPopulatedPlaces.
    for facet_name in PBF_DISPATCH:
        runner.register_handler(
            facet_name=facet_name, module_uri=pbf_uri, entrypoint="handle", timeout_ms=0
        )

    for facet_name in POSTGIS_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=postgis_uri, entrypoint="handle")

    for facet_name in GEOJSON_DISPATCH:
        runner.register_handler(facet_name=facet_name, module_uri=geojson_uri, entrypoint="handle")

    # Overture facets stream remote GeoParquet (cloud-hosted, potentially large
    # bbox windows) in a blocking pyarrow/duckdb loop with sparse heartbeats, so
    # register them with timeout_ms=0 to fall back to the global execution timeout
    # rather than the short per-handler timeout — same rationale as PBF.
    for facet_name in OVERTURE_DISPATCH:
        runner.register_handler(
            facet_name=facet_name, module_uri=overture_uri, entrypoint="handle", timeout_ms=0
        )

    # ohsome-planet facets scan a GeoParquet dataset of the OSM HISTORY, which is
    # larger than any snapshot source here (~150 GB planet history converted), in
    # a blocking pyarrow loop. Same timeout_ms=0 rationale as PBF and Overture.
    for facet_name in OHSOME_DISPATCH:
        runner.register_handler(
            facet_name=facet_name, module_uri=ohsome_uri, entrypoint="handle", timeout_ms=0
        )
