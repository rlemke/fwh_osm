<!-- SPEC TEMPLATE — every specs/<feature>.md follows this shape so the set reads
consistently. Delete this comment in real specs. Keep sections in this order;
omit a section only if it genuinely does not apply (say so in one line rather
than dropping the heading silently). Ground every claim in the actual FFL
docstrings / handler code / tools — do not invent behaviour. -->

# <Feature Name>

**Namespace(s):** `osm.<ns>` · **FFL:** `src/osm_geocoder/handlers/<dir>/ffl/*.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/<dir>/*.py` · **Tools:** `tools/_osm_tools/<...>.py` (if any)

## Overview
One or two paragraphs: what this feature is for, the request it answers, and where
it sits in the pipeline (source → filter → transform → render, etc.).

## How it works
The algorithm / data flow, step by step. Name the concrete steps and the shape of
the data at each (PBF → GeoJSON → tiles → HTML, etc.). If there is a source-adapter
split (extraction vs analysis), say so.

## Fan-out
Does it fan out across the fleet? If yes: what is the fan-out unit (per-region /
per-leaf / per-state / per-city), which facet drives it (a `foreach` over what
list), and why it reduces wall-clock. If it is single-task, say "single-task — no
fan-out" and why (e.g. small input, or atomicity requirement).

## Filtering & attributes
What it filters and on which OSM attributes/tags — be specific
(`amenity=charging_station`, `highway=*`, `boundary=administrative` + `admin_level`,
`building`, etc.). Name the filter mechanism (osmium `tags-filter`, a Python
`ByScript` predicate over `props`, a PostGIS `WHERE`, an Overpass query). If the
feature does no filtering, say so.

## External libraries / binaries
Every non-stdlib dependency this feature relies on and what for — e.g. `osmium`
(osmium-tool binary + pyosmium), `shapely`, `pyproj`, `pyshp`, `folium`,
GraphHopper (Java), Valhalla, `requests`. Distinguish a **binary** dependency from
a **pip** one.

## Facets & workflows
The key event facets and workflows, with signatures and a one-line purpose taken
from the FFL docstrings. Mark event facets (need a handler) vs pure facets, and
note `Effect`/`Cost` mixins where present.

## Cache / output
The cache namespace under `$FW_CACHE_ROOT/<namespace>/` and the cache type, plus the
output artifact(s) and format (GeoJSON / PMTiles / MBTiles / HTML map / PBF / CSV).
Note whether outputs go to local disk, MinIO/S3, or the published site.

## Gotchas & notes
Known pitfalls, rate limits, sensitivity caveats, or non-obvious constraints
(worth capturing anything a future maintainer would trip on).

## Related specs
Links to the specs this feature composes with or depends on.
