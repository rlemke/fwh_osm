"""OSM Geocoder event handlers.

Registers handlers for OSM, Census TIGER, GraphHopper, Valhalla, OSRM,
elevation, and visualization event facets. Modules are organized into
functional subpackages (amenities/, boundaries/, …).
"""

from .amenities.airquality_handlers import register_airquality_handlers
from .amenities.amenity_handlers import register_amenity_handlers
from .boundaries.boundary_handlers import register_boundary_handlers
from .buildings.building_handlers import register_building_handlers
from .cache.region_handlers import register_region_handlers
from .cache.update_handlers import register_update_handlers
from .change.change_handlers import register_change_handlers
from .cities.cities_handlers import register_cities_handlers
from .clip.clip_handlers import register_clip_handlers
from .combined.combined_handlers import register_combined_handlers
from .db.import_handlers import register_import_handlers
from .emergency.emergency_handlers import register_emergency_handlers
from .filters.filter_handlers import register_filter_handlers
from .filters.osmose_handlers import register_osmose_handlers
from .filters.validation_handlers import register_validation_handlers
from .geocoding.geocoding_handlers import register_geocoding_handlers
from .graphhopper.graphhopper_handlers import register_graphhopper_handlers
from .network.network_handlers import register_network_handlers
from .parks.park_handlers import register_park_handlers
from .poi.poi_handlers import register_poi_handlers
from .population.population_handlers import register_population_handlers
from .roads.road_handlers import register_road_handlers
from .roads.zoom_handlers import register_zoom_handlers
from .routes.elevation_handlers import register_elevation_handlers
from .routes.gtfs_handlers import register_gtfs_handlers
from .routes.route_handlers import register_route_handlers
from .routes.routing_handlers import register_routing_handlers
from .routing.routing_adapter_handlers import register_routing_adapter_handlers
from .shared.pbf_cache import download_region  # noqa: F401
from .sources.source_handlers import register_source_handlers
from .spatial.spatial_handlers import register_spatial_handlers
from .tiles.tile_handlers import register_tile_handlers
from .transform.transform_handlers import register_transform_handlers
from .valhalla.valhalla_handlers import register_valhalla_handlers
from .visualization.html_map_handlers import register_html_map_handlers
from .visualization.visualization_handlers import register_visualization_handlers
from .vocab.vocab_handlers import register_vocab_handlers
from .voting.tiger_handlers import register_tiger_handlers

__all__ = [
    "register_all_handlers",
    "register_all_registry_handlers",
    "register_combined_handlers",
    "register_import_handlers",
    "register_airquality_handlers",
    "register_amenity_handlers",
    "register_boundary_handlers",
    "register_building_handlers",
    "register_cities_handlers",
    "register_clip_handlers",
    "register_elevation_handlers",
    "register_emergency_handlers",
    "register_filter_handlers",
    "register_geocoding_handlers",
    "register_graphhopper_handlers",
    "register_gtfs_handlers",
    "register_network_handlers",
    "register_osmose_handlers",
    "register_park_handlers",
    "register_poi_handlers",
    "register_population_handlers",
    "register_region_handlers",
    "register_road_handlers",
    "register_route_handlers",
    "register_routing_handlers",
    "register_tiger_handlers",
    "register_validation_handlers",
    "register_visualization_handlers",
    "register_html_map_handlers",
    "register_zoom_handlers",
    "register_routing_adapter_handlers",
    "register_source_handlers",
    "register_spatial_handlers",
    "register_tile_handlers",
    "register_transform_handlers",
    "register_valhalla_handlers",
    "register_vocab_handlers",
    "download_region",
]


def register_all_handlers(poller) -> None:
    """Register all event facet handlers with the given poller."""
    register_airquality_handlers(poller)
    register_amenity_handlers(poller)
    register_boundary_handlers(poller)
    register_building_handlers(poller)
    register_cities_handlers(poller)
    register_clip_handlers(poller)
    register_elevation_handlers(poller)
    register_emergency_handlers(poller)
    register_filter_handlers(poller)
    register_geocoding_handlers(poller)
    register_graphhopper_handlers(poller)
    register_gtfs_handlers(poller)
    register_network_handlers(poller)
    register_osmose_handlers(poller)
    register_park_handlers(poller)
    register_poi_handlers(poller)
    register_population_handlers(poller)
    register_region_handlers(poller)
    register_update_handlers(poller)
    register_change_handlers(poller)
    register_road_handlers(poller)
    register_route_handlers(poller)
    register_routing_handlers(poller)
    register_tiger_handlers(poller)
    register_valhalla_handlers(poller)
    register_validation_handlers(poller)
    register_visualization_handlers(poller)
    register_html_map_handlers(poller)
    register_zoom_handlers(poller)
    register_vocab_handlers(poller)
    register_combined_handlers(poller)
    register_import_handlers(poller)
    register_source_handlers(poller)
    register_routing_adapter_handlers(poller)
    register_spatial_handlers(poller)
    register_tile_handlers(poller)
    register_transform_handlers(poller)


def register_all_registry_handlers(runner) -> None:
    """Register all event facet handlers with a RegistryRunner."""
    from .amenities.airquality_handlers import register_handlers as reg_airquality
    from .amenities.amenity_handlers import register_handlers as reg_amenity
    from .boundaries.boundary_handlers import register_handlers as reg_boundary
    from .buildings.building_handlers import register_handlers as reg_building
    from .cache.region_handlers import register_handlers as reg_region
    from .cache.update_handlers import register_handlers as reg_update
    from .change.change_handlers import register_handlers as reg_change
    from .cities.cities_handlers import register_handlers as reg_cities
    from .clip.clip_handlers import register_handlers as reg_clip
    from .emergency.emergency_handlers import register_handlers as reg_emergency
    from .filters.filter_handlers import register_handlers as reg_filter
    from .filters.osmose_handlers import register_handlers as reg_osmose
    from .filters.validation_handlers import register_handlers as reg_validation
    from .geocoding.geocoding_handlers import register_handlers as reg_geocoding
    from .graphhopper.graphhopper_handlers import register_handlers as reg_graphhopper
    from .inventory.inventory_handlers import register_handlers as reg_inventory
    from .parks.park_handlers import register_handlers as reg_park
    from .planet.planet_handlers import register_handlers as reg_planet
    from .poi.poi_handlers import register_handlers as reg_poi
    from .query.query_handlers import register_handlers as reg_query
    from .population.population_handlers import register_handlers as reg_population
    from .roads.road_handlers import register_handlers as reg_road
    from .roads.zoom_handlers import register_handlers as reg_zoom
    from .routes.elevation_handlers import register_handlers as reg_elevation
    from .routes.gtfs_handlers import register_handlers as reg_gtfs
    from .routes.route_handlers import register_handlers as reg_route
    from .routes.routing_handlers import register_handlers as reg_routing
    from .valhalla.valhalla_handlers import register_handlers as reg_valhalla
    from .visualization.html_map_handlers import register_handlers as reg_html_map
    from .visualization.visualization_handlers import register_handlers as reg_visualization
    from .voting.tiger_handlers import register_handlers as reg_tiger

    reg_airquality(runner)
    reg_amenity(runner)
    reg_boundary(runner)
    reg_building(runner)
    reg_cities(runner)
    reg_clip(runner)
    reg_elevation(runner)
    reg_emergency(runner)
    reg_filter(runner)
    reg_geocoding(runner)
    reg_graphhopper(runner)
    reg_inventory(runner)
    reg_gtfs(runner)
    reg_osmose(runner)
    reg_park(runner)
    reg_planet(runner)
    reg_poi(runner)
    reg_query(runner)
    reg_population(runner)
    reg_region(runner)
    reg_update(runner)
    reg_change(runner)
    reg_road(runner)
    reg_route(runner)
    reg_routing(runner)
    reg_tiger(runner)
    reg_valhalla(runner)
    reg_validation(runner)
    reg_visualization(runner)
    reg_html_map(runner)
    reg_zoom(runner)

    from .combined.combined_handlers import register_handlers as reg_combined
    from .db.import_handlers import register_handlers as reg_db_import
    from .network.network_handlers import register_handlers as reg_network
    from .routing.routing_adapter_handlers import register_handlers as reg_routing_adapter
    from .sources.source_handlers import register_handlers as reg_source
    from .spatial.spatial_handlers import register_handlers as reg_spatial
    from .tiles.tile_handlers import register_handlers as reg_tile
    from .transform.transform_handlers import register_handlers as reg_transform
    from .vocab.vocab_handlers import register_handlers as reg_vocab

    reg_combined(runner)
    reg_db_import(runner)
    reg_network(runner)
    reg_source(runner)
    reg_routing_adapter(runner)
    reg_spatial(runner)
    reg_tile(runner)
    reg_transform(runner)
    reg_vocab(runner)
