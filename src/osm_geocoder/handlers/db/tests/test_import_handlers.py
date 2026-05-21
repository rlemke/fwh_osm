"""Tests for osm.db.ImportGeoJSON handler."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def geojson_file():
    """Create a temporary GeoJSON FeatureCollection file."""
    features = [
        {
            "type": "Feature",
            "properties": {"osm_id": f"node/{i}", "name": f"Place {i}"},
            "geometry": {"type": "Point", "coordinates": [-86.0 + i * 0.1, 32.0 + i * 0.1]},
        }
        for i in range(5)
    ]
    data = {"type": "FeatureCollection", "features": features}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
        json.dump(data, f)
        path = f.name

    yield path, features
    os.unlink(path)


@pytest.fixture
def empty_geojson_file():
    """Create a temporary empty GeoJSON FeatureCollection file."""
    data = {"type": "FeatureCollection", "features": []}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
        json.dump(data, f)
        path = f.name
    yield path
    os.unlink(path)


class TestSlugify:
    def test_simple(self):
        from osm_geocoder.handlers.db.import_handlers import _slugify

        assert _slugify("Alabama") == "alabama"

    def test_spaces(self):
        from osm_geocoder.handlers.db.import_handlers import _slugify

        assert _slugify("North Carolina") == "north-carolina"

    def test_special_chars(self):
        from osm_geocoder.handlers.db.import_handlers import _slugify

        assert _slugify("São Tomé & Príncipe") == "s-o-tom-pr-ncipe"

    def test_already_slug(self):
        from osm_geocoder.handlers.db.import_handlers import _slugify

        assert _slugify("new-york") == "new-york"


class TestImportHandler:
    def test_missing_file(self):
        from osm_geocoder.handlers.db.import_handlers import _handler

        result = _handler(
            {"output_path": "/nonexistent.geojson", "category": "parks", "region": "test"}
        )
        assert result["imported_count"] == 0

    def test_empty_path(self):
        from osm_geocoder.handlers.db.import_handlers import _handler

        result = _handler({"output_path": "", "category": "parks", "region": "test"})
        assert result["imported_count"] == 0

    @patch("osm_geocoder.handlers.db.osm_store.get_mongo_db")
    def test_import_geojson(self, mock_get_db, geojson_file):
        path, features = geojson_file

        mock_db = MagicMock()
        mock_coll = MagicMock()
        mock_meta = MagicMock()
        mock_db.osm_features = mock_coll
        mock_db.osm_features_meta = mock_meta
        mock_get_db.return_value = mock_db

        from osm_geocoder.handlers.db.import_handlers import _handler

        result = _handler(
            {
                "output_path": path,
                "category": "parks",
                "region": "Alabama",
                "feature_count": 5,
            }
        )

        assert result["imported_count"] == 5
        assert result["dataset_key"] == "osm.parks.alabama"
        assert result["collection"] == "osm_features"
        assert mock_coll.bulk_write.called

    @patch("osm_geocoder.handlers.db.osm_store.get_mongo_db")
    def test_import_empty_geojson(self, mock_get_db, empty_geojson_file):
        mock_db = MagicMock()
        mock_coll = MagicMock()
        mock_meta = MagicMock()
        mock_db.osm_features = mock_coll
        mock_db.osm_features_meta = mock_meta
        mock_get_db.return_value = mock_db

        from osm_geocoder.handlers.db.import_handlers import _handler

        result = _handler(
            {
                "output_path": empty_geojson_file,
                "category": "parks",
                "region": "Delaware",
                "feature_count": 0,
            }
        )

        assert result["imported_count"] == 0
        assert result["dataset_key"] == "osm.parks.delaware"

    @patch("osm_geocoder.handlers.db.osm_store.get_mongo_db")
    def test_step_log_called(self, mock_get_db, geojson_file):
        path, _ = geojson_file

        mock_db = MagicMock()
        mock_db.osm_features = MagicMock()
        mock_db.osm_features_meta = MagicMock()
        mock_get_db.return_value = mock_db

        step_log = MagicMock()

        from osm_geocoder.handlers.db.import_handlers import _handler

        _handler(
            {
                "output_path": path,
                "category": "boundaries",
                "region": "Alaska",
                "feature_count": 5,
                "_step_log": step_log,
            }
        )

        assert step_log.call_count >= 2  # start + success

    @patch("osm_geocoder.handlers.db.osm_store.get_mongo_db")
    def test_dataset_key_no_category(self, mock_get_db, geojson_file):
        path, _ = geojson_file

        mock_db = MagicMock()
        mock_db.osm_features = MagicMock()
        mock_db.osm_features_meta = MagicMock()
        mock_get_db.return_value = mock_db

        from osm_geocoder.handlers.db.import_handlers import _handler

        result = _handler(
            {
                "output_path": path,
                "category": "",
                "region": "Alabama",
                "feature_count": 5,
            }
        )

        assert result["dataset_key"] == "osm.alabama"


class TestOsmStore:
    @patch("osm_geocoder.handlers.db.osm_store.get_mongo_db")
    def test_import_geojson_batching(self, mock_get_db, geojson_file):
        """Verify bulk_write is called with correct batch sizes."""
        path, features = geojson_file

        mock_db = MagicMock()
        mock_coll = MagicMock()
        mock_meta = MagicMock()
        mock_db.osm_features = mock_coll
        mock_db.osm_features_meta = mock_meta
        mock_get_db.return_value = mock_db

        from osm_geocoder.handlers.db.osm_store import import_geojson

        result = import_geojson(
            path=path,
            dataset_key="osm.parks.test",
            category="parks",
            region="test",
            db=mock_db,
        )

        assert result["imported_count"] == 5
        # 5 features < BATCH_SIZE (1000), so one bulk_write call
        assert mock_coll.bulk_write.call_count == 1

    def test_ensure_indexes(self):
        mock_coll = MagicMock()

        from osm_geocoder.handlers.db.osm_store import ensure_indexes

        ensure_indexes(mock_coll)

        assert mock_coll.create_index.call_count == 3

    @patch("osm_geocoder.handlers.db.osm_store.get_mongo_db")
    def test_metadata_written(self, mock_get_db, geojson_file):
        path, _ = geojson_file

        mock_db = MagicMock()
        mock_db.osm_features = MagicMock()
        mock_meta = MagicMock()
        mock_db.osm_features_meta = mock_meta
        mock_get_db.return_value = mock_db

        from osm_geocoder.handlers.db.osm_store import import_geojson

        import_geojson(
            path=path,
            dataset_key="osm.parks.test",
            category="parks",
            region="test",
            db=mock_db,
        )

        mock_meta.replace_one.assert_called_once()
        meta_doc = mock_meta.replace_one.call_args[0][1]
        assert meta_doc["dataset_key"] == "osm.parks.test"
        assert meta_doc["feature_count"] == 5
        assert meta_doc["category"] == "parks"


class TestRegistration:
    def test_register_import_handlers(self):
        from osm_geocoder.handlers.db.import_handlers import register_import_handlers

        poller = MagicMock()
        register_import_handlers(poller)
        poller.register.assert_called_once_with("osm.db.ImportGeoJSON", _handler_ref())

    def test_register_handlers_registry(self):
        from osm_geocoder.handlers.db.import_handlers import register_handlers

        runner = MagicMock()
        register_handlers(runner)
        runner.register_handler.assert_called_once()
        call_kwargs = runner.register_handler.call_args
        assert call_kwargs[1]["facet_name"] == "osm.db.ImportGeoJSON"

    def test_handle_dispatch(self):
        from osm_geocoder.handlers.db.import_handlers import handle

        with pytest.raises(ValueError, match="Unknown facet"):
            handle({"_facet_name": "osm.db.NonExistent"})


def _handler_ref():
    """Get a reference to the handler function for assertion matching."""
    from osm_geocoder.handlers.db.import_handlers import _handler

    return _handler
