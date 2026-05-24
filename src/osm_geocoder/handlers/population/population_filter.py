"""Population-based filtering for OSM data.

Filters places and administrative boundaries by population.
Supports cities, towns, villages, states, countries, counties, etc.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from facetwork.runtime.storage import get_storage_backend

from ..shared._output import ensure_dir, open_output, resolve_output_dir, uri_stem

_storage = get_storage_backend()

log = logging.getLogger(__name__)

import importlib.util

HAS_OSMIUM = importlib.util.find_spec("osmium") is not None


class PlaceType(Enum):
    """Supported place types for population filtering."""

    CITY = "city"
    TOWN = "town"
    VILLAGE = "village"
    HAMLET = "hamlet"
    SUBURB = "suburb"
    COUNTRY = "country"
    STATE = "state"
    COUNTY = "county"
    MUNICIPALITY = "municipality"
    ALL = "all"

    @classmethod
    def from_string(cls, value: str) -> "PlaceType":
        """Parse a place type string (case-insensitive)."""
        normalized = value.lower().strip()
        aliases = {
            "city": cls.CITY,
            "cities": cls.CITY,
            "town": cls.TOWN,
            "towns": cls.TOWN,
            "village": cls.VILLAGE,
            "villages": cls.VILLAGE,
            "hamlet": cls.HAMLET,
            "hamlets": cls.HAMLET,
            "suburb": cls.SUBURB,
            "suburbs": cls.SUBURB,
            "country": cls.COUNTRY,
            "countries": cls.COUNTRY,
            "nation": cls.COUNTRY,
            "state": cls.STATE,
            "states": cls.STATE,
            "province": cls.STATE,
            "provinces": cls.STATE,
            "county": cls.COUNTY,
            "counties": cls.COUNTY,
            "municipality": cls.MUNICIPALITY,
            "municipalities": cls.MUNICIPALITY,
            "all": cls.ALL,
            "*": cls.ALL,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown place type: {value}")


class Operator(Enum):
    """Comparison operators for population filtering."""

    GT = "gt"  # Greater than
    GTE = "gte"  # Greater than or equal
    LT = "lt"  # Less than
    LTE = "lte"  # Less than or equal
    EQ = "eq"  # Equal
    NE = "ne"  # Not equal
    BETWEEN = "between"  # Between min and max (inclusive)

    @classmethod
    def from_string(cls, value: str) -> "Operator":
        """Parse an operator string (case-insensitive)."""
        normalized = value.lower().strip()
        aliases = {
            "gt": cls.GT,
            ">": cls.GT,
            "gte": cls.GTE,
            ">=": cls.GTE,
            "lt": cls.LT,
            "<": cls.LT,
            "lte": cls.LTE,
            "<=": cls.LTE,
            "eq": cls.EQ,
            "=": cls.EQ,
            "==": cls.EQ,
            "ne": cls.NE,
            "!=": cls.NE,
            "<>": cls.NE,
            "between": cls.BETWEEN,
            "range": cls.BETWEEN,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown operator: {value}")


# OSM tag configurations for place types
# Maps PlaceType to the tags used to identify them
PLACE_TAGS = {
    PlaceType.CITY: {
        "place": ["city"],
    },
    PlaceType.TOWN: {
        "place": ["town"],
    },
    PlaceType.VILLAGE: {
        "place": ["village"],
    },
    PlaceType.HAMLET: {
        "place": ["hamlet"],
    },
    PlaceType.SUBURB: {
        "place": ["suburb", "neighbourhood", "quarter"],
    },
    PlaceType.COUNTRY: {
        "place": ["country"],
        "boundary": ["administrative"],
        "admin_level": ["2"],
    },
    PlaceType.STATE: {
        "place": ["state", "province"],
        "boundary": ["administrative"],
        "admin_level": ["4"],
    },
    PlaceType.COUNTY: {
        "place": ["county"],
        "boundary": ["administrative"],
        "admin_level": ["6"],
    },
    PlaceType.MUNICIPALITY: {
        "place": ["municipality"],
        "boundary": ["administrative"],
        "admin_level": ["8"],
    },
}


@dataclass
class PopulationFilteredFeatures:
    """Result of a population filter operation."""

    output_path: str
    feature_count: int
    original_count: int
    place_type: str
    min_population: int
    max_population: int
    filter_applied: str
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class PopulationStats:
    """Statistics for population data."""

    total_places: int
    total_population: int
    min_population: int
    max_population: int
    avg_population: int
    place_type: str


def parse_population(value: str | int | None) -> int | None:
    """Parse a population value from OSM tags.

    Handles various formats like "1234", "1,234", "1.234", "~1000", etc.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value

    # Clean up the string
    cleaned = str(value).strip()
    if not cleaned:
        return None

    # Remove common prefixes/suffixes
    cleaned = cleaned.lstrip("~≈≅").rstrip("+")

    # Remove thousands separators (comma or period depending on locale)
    # Try comma first (English), then period (European)
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "." in cleaned and "," not in cleaned:
        # Could be European format or decimal - check if it looks like thousands
        parts = cleaned.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and parts[1].isdigit():
            # Likely European thousands separator (e.g., "1.234" = 1234)
            cleaned = cleaned.replace(".", "")

    # Remove any remaining non-numeric characters except minus
    result = ""
    for c in cleaned:
        if c.isdigit() or (c == "-" and not result):
            result += c

    if not result or result == "-":
        return None

    try:
        return int(result)
    except ValueError:
        return None


def extract_places_with_population(
    pbf_path: str | Path,
    place_type: str | PlaceType = "all",
    min_population: int = 0,
    output_path: str | Path | None = None,
    heartbeat=None,
    task_uuid: str = "",
) -> PopulationFilteredFeatures:
    """Extract populated places from a PBF as GeoJSON Points.

    Walks nodes carrying a ``place`` or ``population`` tag, keeps those matching
    the requested ``place_type`` (``city``/``town``/``village``/… or ``all``)
    via :func:`matches_place_type`, and — when ``min_population > 0`` — only
    those whose parsed ``population`` meets the threshold. Every OSM tag is
    preserved (``name``, ``place``, ``population`` …). ``original_count`` is the
    place candidates before the population filter; ``max_population`` is the
    largest kept value.
    """
    import os
    import re
    import shutil
    import tempfile

    import osmium
    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    from ..shared.geojson_writer import GeoJSONStreamWriter

    pbf_path = str(pbf_path)
    ptype = PlaceType.from_string(place_type) if isinstance(place_type, str) else place_type
    min_pop = int(min_population or 0)

    if output_path is None:
        out_dir = resolve_output_dir("osm-population")
        output_path_str = f"{out_dir}/{uri_stem(pbf_path)}_places_{ptype.value}.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    local_pbf = localize(pbf_path)

    def _parse_pop(value: str) -> int | None:
        digits = re.sub(r"[^0-9]", "", value or "")
        return int(digits) if digits else None

    class _PlaceHandler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.kept = 0
            self.original = 0
            self.max_pop = 0

        def node(self, n) -> None:
            if not n.location.valid():
                return
            tags = {t.k: t.v for t in n.tags}
            if "place" not in tags and "population" not in tags:
                return
            if not matches_place_type(tags, ptype):
                return
            self.original += 1
            pop = _parse_pop(tags.get("population", ""))
            if min_pop > 0 and (pop is None or pop < min_pop):
                return
            props = dict(tags)
            props["osm_id"] = n.id
            if pop is not None:
                props["population_value"] = pop
                self.max_pop = max(self.max_pop, pop)
            self.writer.write_feature(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": {"type": "Point", "coordinates": [n.location.lon, n.location.lat]},
                }
            )
            self.kept += 1
            if heartbeat and self.kept % 5000 == 0:
                heartbeat()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            handler = _PlaceHandler(writer)
            handler.apply_file(local_pbf, locations=True, idx="flex_mem")
        ensure_dir(output_path_str)
        shutil.move(tmp_path, output_path_str)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    filter_applied = (
        f"place_type={ptype.value}, population>={min_pop}"
        if min_pop > 0
        else f"place_type={ptype.value}"
    )
    return PopulationFilteredFeatures(
        output_path=output_path_str,
        feature_count=handler.kept,
        original_count=handler.original,
        place_type=ptype.value,
        min_population=min_pop,
        max_population=handler.max_pop,
        filter_applied=filter_applied,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def matches_place_type(tags: dict[str, str], place_type: PlaceType) -> bool:
    """Check if tags match the specified place type."""
    if place_type == PlaceType.ALL:
        # Match any place with a population tag
        return "population" in tags

    config = PLACE_TAGS.get(place_type, {})

    # Check place tag
    if "place" in config:
        place_value = tags.get("place", "")
        if place_value in config["place"]:
            return True

    # Check boundary + admin_level combination
    if "boundary" in config and "admin_level" in config:
        boundary = tags.get("boundary", "")
        admin_level = tags.get("admin_level", "")
        if boundary in config["boundary"] and admin_level in config["admin_level"]:
            return True

    return False


def matches_population(
    population: int, min_pop: int, max_pop: int | None, operator: Operator
) -> bool:
    """Check if population matches the filter criteria."""
    if operator == Operator.GT:
        return population > min_pop
    elif operator == Operator.GTE:
        return population >= min_pop
    elif operator == Operator.LT:
        return population < min_pop
    elif operator == Operator.LTE:
        return population <= min_pop
    elif operator == Operator.EQ:
        return population == min_pop
    elif operator == Operator.NE:
        return population != min_pop
    elif operator == Operator.BETWEEN:
        if max_pop is None:
            raise ValueError("BETWEEN operator requires max_population")
        return min_pop <= population <= max_pop
    return False


def describe_filter(
    place_type: PlaceType, min_pop: int, max_pop: int | None, operator: Operator
) -> str:
    """Generate a human-readable description of the filter."""
    type_str = place_type.value if place_type != PlaceType.ALL else "all places"

    if operator == Operator.BETWEEN:
        return f"{type_str} with population between {min_pop:,} and {max_pop:,}"

    op_map = {
        Operator.GT: f"> {min_pop:,}",
        Operator.GTE: f">= {min_pop:,}",
        Operator.LT: f"< {min_pop:,}",
        Operator.LTE: f"<= {min_pop:,}",
        Operator.EQ: f"= {min_pop:,}",
        Operator.NE: f"!= {min_pop:,}",
    }

    return f"{type_str} with population {op_map.get(operator, '')}"


def filter_geojson_by_population(
    input_path: str | Path,
    min_population: int,
    max_population: int | None = None,
    place_type: str | PlaceType = PlaceType.ALL,
    operator: str | Operator = Operator.GTE,
    output_path: str | Path | None = None,
) -> PopulationFilteredFeatures:
    """Filter a GeoJSON file by population.

    Args:
        input_path: Path to input GeoJSON file
        min_population: Minimum population threshold
        max_population: Maximum population (for BETWEEN operator)
        place_type: Type of place to filter
        operator: Comparison operator
        output_path: Path to output GeoJSON file

    Returns:
        PopulationFilteredFeatures with output path and statistics
    """
    input_path = str(input_path)

    # Parse place type and operator
    if isinstance(place_type, str):
        place_type = PlaceType.from_string(place_type)
    if isinstance(operator, str):
        operator = Operator.from_string(operator)

    # Generate output path if not provided
    if output_path is None:
        out_dir = resolve_output_dir("osm-population")
        suffix = f"_pop_{min_population}"
        if max_population is not None:
            suffix += f"_{max_population}"
        output_path_str = f"{out_dir}/{uri_stem(input_path)}{suffix}.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    # Load input GeoJSON
    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    original_count = len(features)

    # Filter features
    filtered = []
    for feature in features:
        props = feature.get("properties", {})

        # Check place type
        if not matches_place_type(props, place_type):
            continue

        # Parse population
        population = parse_population(props.get("population"))
        if population is None:
            continue

        # Check population threshold
        if matches_population(population, min_population, max_population, operator):
            filtered.append(feature)

    # Build output GeoJSON
    output_geojson = {
        "type": "FeatureCollection",
        "features": filtered,
    }

    # Write output
    with open_output(output_path_str) as f:
        json.dump(output_geojson, f, indent=2)

    return PopulationFilteredFeatures(
        output_path=output_path_str,
        feature_count=len(filtered),
        original_count=original_count,
        place_type=place_type.value,
        min_population=min_population,
        max_population=max_population if max_population is not None else 0,
        filter_applied=describe_filter(place_type, min_population, max_population, operator),
        extraction_date=datetime.now(UTC).isoformat(),
    )


def calculate_population_stats(
    input_path: str | Path,
    place_type: str | PlaceType = PlaceType.ALL,
) -> PopulationStats:
    """Calculate population statistics for a GeoJSON file.

    Args:
        input_path: Path to GeoJSON file
        place_type: Type of place to include in stats

    Returns:
        PopulationStats with counts and totals
    """
    input_path = str(input_path)

    # Parse place type
    if isinstance(place_type, str):
        place_type = PlaceType.from_string(place_type)

    # Load GeoJSON
    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    populations = []
    for feature in features:
        props = feature.get("properties", {})

        # Check place type
        if not matches_place_type(props, place_type):
            continue

        # Parse population
        population = parse_population(props.get("population"))
        if population is not None and population > 0:
            populations.append(population)

    if not populations:
        return PopulationStats(
            total_places=0,
            total_population=0,
            min_population=0,
            max_population=0,
            avg_population=0,
            place_type=place_type.value,
        )

    return PopulationStats(
        total_places=len(populations),
        total_population=sum(populations),
        min_population=min(populations),
        max_population=max(populations),
        avg_population=sum(populations) // len(populations),
        place_type=place_type.value,
    )
