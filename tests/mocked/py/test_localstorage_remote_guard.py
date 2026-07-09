"""LocalStorage must refuse remote URIs.

Regression for the bug where an ``s3://…`` path reached LocalStorage and
``os.makedirs`` collapsed it to a local ``s3:/…`` directory (a stub of which
was once committed to the repo). LocalStorage now fails loudly instead.
"""
import pytest

from osm_geocoder.tools._osm_tools.storage import LocalStorage


@pytest.mark.parametrize(
    "path",
    [
        "s3://afl-cache/cache/osm/geojson/europe/germany-latest.geojson",
        "s3:/afl-cache/cache/osm",          # already-collapsed single-slash form
        "hdfs://namenode:9000/osm/x",
        "hdfs:/osm/x",
    ],
)
def test_reject_remote_uri(path):
    s = LocalStorage()
    with pytest.raises(ValueError):
        s.mkdir_p(path)
    with pytest.raises(ValueError):
        s.write_text_atomic(path, "{}")
    with pytest.raises(ValueError):
        s.exists(path)


def test_local_paths_still_allowed(tmp_path):
    s = LocalStorage()
    p = str(tmp_path / "sub" / "out.geojson")
    s.write_text_atomic(p, '{"type":"FeatureCollection","features":[]}')
    assert s.exists(p)
    assert s.read_text(p).startswith("{")
