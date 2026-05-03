"""Handler-side re-export of the shared PBF conversion libraries.

The real implementations live in ``tools/_lib/pbf_geojson.py`` and
``tools/_lib/pbf_shapefile.py``. Both the CLI tools
(``convert-pbf-geojson``, ``convert-pbf-shapefile``) and the FFL
``osm.ops.ConvertPbfToGeoJson`` / ``ConvertPbfToShapefile`` handlers
call into those libraries, so they share one on-disk layout and one
manifest per cache type.

``shared.pbf_cache`` already performs the ``sys.path`` adjustment that
makes the ``tools/`` directory importable; this module relies on that
side effect and adds its own just in case ``pbf_cache`` wasn't loaded
first.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

# Re-export both conversion libraries. Callers use aliased names to avoid
# name collisions between the two ``convert_region`` / ``ConvertResult`` /
# ``to_osm_cache`` / ``ConversionError`` symbols.
from _lib import graphhopper_build as graphhopper  # noqa: E402,F401
from _lib import html_render  # noqa: E402,F401
from _lib import pbf_extract as extract  # noqa: E402,F401
from _lib import pbf_geojson as geojson  # noqa: E402,F401
from _lib import pbf_shapefile as shapefile  # noqa: E402,F401
from _lib import valhalla_build as valhalla  # noqa: E402,F401
