"""Handler-side re-export of the shared PBF cache library.

The real implementation lives in ``examples/osm-geocoder/tools/_lib/``. It
is shared verbatim by:

- the ``download-pbf`` CLI tool (``examples/osm-geocoder/tools/``), and
- the FFL ``osm.ops.CacheRegion`` / ``ResolveRegion`` / PostGIS import
  handlers (this package).

Both entry points call ``download_region`` here, so they end up reading
and writing the same on-disk PBF cache
(``$AFL_DATA_ROOT/cache/osm/pbf/...``) and the same per-entry
``.meta.json`` sidecars — the tool and the FFL are two surfaces onto one
cache.

This module performs a one-time ``sys.path`` adjustment so handlers can
import the library with a natural ``from ..shared.pbf_cache import ...``
without every caller repeating the path gymnastics.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _lib.pbf_download import (  # noqa: E402,F401
    DownloadError,
    DownloadResult,
    cached_path,
    download_region,
    is_region_cached,
    manifest_entry_for,
    sidecar_entry_for,
    to_osm_cache,
)
from _lib.storage import LocalStorage, Storage, get_storage  # noqa: E402,F401
