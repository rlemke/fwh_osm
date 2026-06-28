"""Opt-in S3/MinIO test for the osm.Network cache + output round-trip.

Gated on ``FW_S3_ENDPOINT`` (a reachable S3/MinIO). Verifies that with the
durable roots on ``s3://`` the build cache, the sidecar, the read-back loader,
and a route-layer output all live on the object store — i.e. the step-payload
paths a fleet shares are portable, not local. Start MinIO with::

    docker run -d -p 9000:9000 -e MINIO_ROOT_USER=minioadmin \\
        -e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data
    export FW_STORAGE=s3 FW_DATA_ROOT=s3://afl-cache \\
        FW_S3_ENDPOINT=http://localhost:9000 \\
        FW_S3_ACCESS_KEY=minioadmin FW_S3_SECRET_KEY=minioadmin \\
        FW_OSM_OUTPUT_BASE=s3://afl-cache/osm-output
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("boto3")
pytest.importorskip("shapely")
pytest.importorskip("networkx")

pytestmark = pytest.mark.skipif(
    not os.environ.get("FW_S3_ENDPOINT") or os.environ.get("FW_STORAGE") != "s3",
    reason="set FW_STORAGE=s3 + FW_S3_ENDPOINT (and s3:// roots) to run the live S3 cache test",
)

from osm_geocoder.handlers.network import network_ops as ops  # noqa: E402


def test_node_id_network_cache_and_output_on_s3(tmp_path):
    from facetwork.runtime.storage import get_storage_backend

    # synthetic node-id roads: two ways sharing node 200 -> one component
    feats = [
        {"type": "Feature", "properties": {"ref": "A1", "node_ids": [100, 200]},
         "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}},
        {"type": "Feature", "properties": {"ref": "A1", "node_ids": [200, 300]},
         "geometry": {"type": "LineString", "coordinates": [[1, 0], [2, 0]]}},
    ]
    src = tmp_path / "roads_nodeid.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    res = ops.build_network(str(src))
    assert res.network_path.startswith("s3://")
    assert res.network_path.endswith("@nodeid")
    assert res.connected_components == 1

    b = get_storage_backend("s3://x")
    for f in ("graph.json", "nodes.geojson", "edges.geojson"):
        assert b.exists(b.join(res.network_path, f))
    assert b.exists(res.network_path + ".meta.json")

    # load back from s3 + route -> output also lands on s3
    ops._GRAPH_CACHE.clear()
    out = ops.route_layer(res.network_path, points=json.dumps([[0.0, 0.0, "A"], [2.0, 0.0, "B"]]))
    assert out.output_path.startswith("s3://")
    assert b.exists(out.output_path)
    layer = json.loads(b.open(out.output_path).read())
    assert layer["features"][0]["properties"]["reached_b"] is True
