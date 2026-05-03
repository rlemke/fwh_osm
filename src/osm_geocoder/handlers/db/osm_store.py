"""OSM feature store — streams GeoJSON into MongoDB with 2dsphere indexing.

Uses the shared ``iter_geojson_features()`` streaming reader so that
multi-GB files never need to fit in memory.  Features are bulk-upserted
with a compound unique key ``(dataset_key, feature_key)`` to allow
idempotent re-imports.

Collection: ``osm_features`` in the ``AFL_EXAMPLES_DATABASE`` database
(default ``facetwork_examples``), keeping OSM data isolated from the
Facetwork runtime database.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from pymongo import MongoClient, ReplaceOne
from pymongo.collection import Collection
from pymongo.database import Database

log = logging.getLogger(__name__)

BATCH_SIZE = 1000


def get_mongo_db() -> Database:
    """Connect to MongoDB for OSM feature storage."""
    url = os.environ.get("AFL_MONGODB_URL", "mongodb://afl-mongodb:27017")
    db_name = os.environ.get("AFL_EXAMPLES_DATABASE", "facetwork_examples")
    return MongoClient(url)[db_name]


def ensure_indexes(coll: Collection) -> None:
    """Create indexes on the osm_features collection (idempotent)."""
    coll.create_index(
        [("dataset_key", 1), ("feature_key", 1)],
        unique=True,
        name="osm_upsert_key",
    )
    coll.create_index(
        [("geometry", "2dsphere")],
        sparse=True,
        name="osm_geo_2dsphere",
    )
    coll.create_index("dataset_key", name="osm_dataset_key")


def import_geojson(
    path: str,
    dataset_key: str,
    category: str,
    region: str,
    *,
    heartbeat: callable | None = None,
    heartbeat_interval: float = 30.0,
    db: Database | None = None,
) -> dict[str, Any]:
    """Stream a GeoJSON file into the ``osm_features`` collection.

    Args:
        path: Path to a GeoJSON FeatureCollection file.
        dataset_key: Compound key like ``osm.parks.alabama``.
        category: OSM category (e.g. ``parks``, ``boundaries``).
        region: Region slug (e.g. ``alabama``).
        heartbeat: Optional callback for long-running imports.
        heartbeat_interval: Seconds between heartbeat calls.
        db: Optional pre-connected Database (uses ``get_mongo_db()`` if None).

    Returns:
        Dict with ``imported_count``, ``dataset_key``, ``collection``.
    """
    from ..shared.geojson_writer import iter_geojson_features

    if db is None:
        db = get_mongo_db()

    coll = db.osm_features
    ensure_indexes(coll)

    now = int(time.time() * 1000)
    ops: list[ReplaceOne] = []
    count = 0
    last_hb = time.monotonic()

    for feat in iter_geojson_features(
        path, heartbeat=heartbeat, heartbeat_interval=heartbeat_interval
    ):
        props = feat.get("properties", {})
        # Use osm_id if available, fall back to sequential index
        feature_key = str(props.get("osm_id", props.get("id", count)))

        doc: dict[str, Any] = {
            "dataset_key": dataset_key,
            "feature_key": feature_key,
            "category": category,
            "region": region,
            "properties": props,
            "geometry": feat.get("geometry"),
            "imported_at": now,
        }
        ops.append(
            ReplaceOne(
                {"dataset_key": dataset_key, "feature_key": feature_key},
                doc,
                upsert=True,
            )
        )
        count += 1

        if len(ops) >= BATCH_SIZE:
            coll.bulk_write(ops, ordered=False)
            ops = []
            # Heartbeat between batches
            if heartbeat and time.monotonic() - last_hb > heartbeat_interval:
                heartbeat()
                last_hb = time.monotonic()

    if ops:
        coll.bulk_write(ops, ordered=False)

    # Update metadata
    db.osm_features_meta.replace_one(
        {"dataset_key": dataset_key},
        {
            "dataset_key": dataset_key,
            "category": category,
            "region": region,
            "feature_count": count,
            "source_path": path,
            "imported_at": now,
        },
        upsert=True,
    )

    log.info("Imported %d features into osm_features (dataset_key=%s)", count, dataset_key)
    return {
        "imported_count": count,
        "dataset_key": dataset_key,
        "collection": "osm_features",
    }
