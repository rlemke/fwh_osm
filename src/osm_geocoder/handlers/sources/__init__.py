"""Source adapter handlers for OSM data extraction.

Provides a unified interface for extracting OSM features from three data sources:
- PBF: Direct extraction from .osm.pbf files via osmium
- PostGIS: SQL queries against imported osm_nodes/osm_ways tables
- GeoJSON: Loading and filtering existing GeoJSON files
"""
