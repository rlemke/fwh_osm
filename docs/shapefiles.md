# Shapefiles — reading ESRI shapefiles (Census TIGER) into GeoJSON

**Namespace(s):** `osm.Europe.shapefiles` (legacy FFL) ·
**FFL:** `src/osm_geocoder/handlers/shapefiles/ffl/osmshapefiles.ffl` (+ README) ·
**Handlers:** *none in this dir* — the live shapefile-reading code is the tool below ·
**Tools:** `tools/_osm_tools/tiger_fetch.py` (read ESRI → GeoJSON, pyshp) ·
`tools/_osm_tools/pbf_shapefile.py` (write PBF → ESRI, ogr2ogr)

## Overview

Two independent shapefile concerns live under this feature, in opposite
directions:

1. **Reading ESRI shapefiles → GeoJSON** (`tiger_fetch.py`, pyshp) — the important,
   live one. US Census **TIGER/Line** `STATE` and `COUNTY` shapefiles are the
   sub-national boundary source for the [planet-extraction](planet-extraction.md)
   pipeline. osmfr's `/polygons/` tree has **no** per-US-state polygons (it splits
   the US into four macro-regions) and Geofabrik's `.poly` host is IP-banned, so US
   state/county boundaries come from TIGER, converted to GeoJSON polygons that
   `osmium extract` can clip against.
2. **Writing PBF → ESRI shapefiles** (`pbf_shapefile.py`, ogr2ogr) — the reverse:
   converting a cached PBF into a multi-layer shapefile bundle
   (`osm.ops.ConvertPbfToShapefile`).

The `handlers/shapefiles/` dir itself is now **FFL + README only**. Its
`osm.Europe.shapefiles.EuropeShapefiles` facet and the `DownloadShapefile` verb it
calls describe a legacy flow (fetch Geofabrik `.free.shp.zip` alongside the PBF);
the shared downloader that backed `DownloadShapefile` has since been removed, so
this FFL is historical — treat `tiger_fetch.py` as the feature's real substance.

## How it works

`tiger_fetch.py` turns a Census TIGER shapefile ZIP into one GeoJSON polygon per
administrative unit, keyed Geofabrik-style so the extracts resolve against normal
region requests:

- **`fetch_tiger_states(dest)`** — downloads `tl_<year>_us_state.zip`, unzips it in
  memory, opens the `.shp` with **pyshp** (`shapefile.Reader`), and writes one
  GeoJSON `Feature` per state using pyshp's `shape.__geo_interface__` for geometry.
  Each state is keyed `north-america/us/<state-slug>` (e.g.
  `north-america/us/california`). `admin_level=4`.
- **`fetch_tiger_counties(dest, only_state=…)`** — downloads
  `tl_<year>_us_county.zip`, and first builds a `{STATEFP: state-slug}` map from the
  state shapefile so each county can be **nested under its parent state**:
  `north-america/us/<state-slug>/<county-slug>`. `admin_level=6`. `only_state` (a
  state slug) restricts output to one state's counties — the per-state fan-out unit
  the planet pipeline uses.

Each output is a single-`Feature` GeoJSON file named
`<key-with-slashes→__>.geojson`; `osmium extract` reads GeoJSON as readily as a
`.poly`, so these become extraction polygons directly. The extraction source (the
planet, or the cheaper `north-america` continent extract) is the caller's choice.

### County-suffix normalization

TIGER's county `NAME` is usually bare (`"Alachua"`) but sometimes carries the
admin type (`"Aleutians East Borough"`). Both TIGER and the self-generated
OSM-boundary path (`boundary_gen`) must produce the **same** bare slug, or the
fallback publishes a duplicate per county — one `<x>-county` (from the OSM name
`"Alachua County"`) and one `<x>` (from TIGER), a ~2× dupe. So both call
`boundary_gen._strip_admin_type`, a regex dropping the trailing US county-type
words:

```
(county | parish | census area | city and borough | borough | municipality)$
```

covering County (most states), Parish (LA), and the Alaskan Borough / Census Area /
Municipality / City-and-Borough forms. The suffix is harmless elsewhere — no other
country's level-6 name ends in these words.

### Territory filtering

Geofabrik's `us/` set is the 50 states + DC. TIGER includes the territories, so
`_statefp_slugs` drops the territory FIPS codes `_SKIP_STATEFP = {60 (AS), 66 (GU),
69 (MP), 72 (PR), 78 (VI)}`. Counties whose `STATEFP` doesn't map to a kept state
slug are skipped, so American Samoa / Guam / Northern Marianas / Puerto Rico / US
Virgin Islands never enter the tree.

## Fan-out

`tiger_fetch` itself is a library — it fans out at the **planet-extraction** layer,
where US counties are extracted **per state** (`only_state=<slug>`, 51
`BuildAdminSet(admin_level=6)` tasks) across the fleet (see
[planet-extraction](planet-extraction.md) §Fan-out). The legacy
`osm.Europe.shapefiles.EuropeShapefiles` facet was a static inline fan-out (every
European country listed, merged with `++`), not a `foreach`.

## Filtering & attributes

Shapefile reading does **no OSM-tag filtering** — TIGER is Census data, not OSM,
and every state/county polygon is kept (minus the skipped territories). The only
attributes read from the shapefile records are `STATEFP` (to key counties under
states and drop territories) and `NAME` (slugified, admin-type stripped). The
admin-level semantics (state = 4, county = 6) come from which TIGER layer is
fetched, not from a tag.

## External libraries / binaries

- **`pyshp`** (pip `shapefile`) — reads the ESRI `.shp`/`.dbf` records and yields
  GeoJSON geometry via `__geo_interface__`. A missing pyshp raises a clear
  `TigerError` before the download. A **pip** dependency (no binary).
- **stdlib** (`urllib`, `zipfile`, `io`, `json`, `re`) — download, in-memory unzip,
  GeoJSON write, slug/suffix regexes. No network library beyond stdlib.
- **`ogr2ogr` (GDAL binary)** — used only by the *reverse* direction
  (`pbf_shapefile.py`, PBF → multi-layer ESRI bundle), not by TIGER reading.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `fetch_tiger_states(dest)` | tool fn | TIGER STATE `.shp` → per-state GeoJSON polys (`admin_level=4`), keyed `north-america/us/<state>` |
| `fetch_tiger_counties(dest, only_state)` | tool fn | TIGER COUNTY `.shp` → per-county GeoJSON polys (`admin_level=6`), nested `north-america/us/<state>/<county>` |
| `osm.Europe.shapefiles.EuropeShapefiles()` | facet (legacy) | Cache each European country + `DownloadShapefile` its `.free.shp.zip` — **handler removed**, historical |
| `osm.ops.ConvertPbfToShapefile` | event (reverse) | Cached PBF → multi-layer ESRI shapefile bundle via ogr2ogr |

The related `osm.voting.ShapefileToGeoJSON(cache: TIGERCache)` facet (voting
namespace) reuses the same TIGER-shapefile-to-GeoJSON idea for congressional/voting
districts.

## Cache / output

- **TIGER GeoJSON polys** are written to the `dest` dir the planet pipeline passes
  (scratch), one `Feature` file per unit — consumed immediately by `osmium extract`,
  not a long-lived cache.
- **TIGER source URLs** are `https://www2.census.gov/geo/tiger/TIGER<year>/…`,
  configurable via `FW_TIGER_YEAR` (default 2023), `FW_TIGER_STATE_URL`,
  `FW_TIGER_COUNTY_URL`.
- **PBF → shapefile** output is a **directory** of shapefile bundles (one
  `.shp/.shx/.dbf/.prj/.cpg` set per layer) with a sibling sidecar; the
  `other_relations` (GeometryCollection) layer is never produced — shapefile can't
  represent it.

## Gotchas & notes

- **The `handlers/shapefiles/` dir has no Python.** Its README describes a
  `DownloadShapefile` handler in an `operations_handlers.py` that no longer exists;
  the shared downloader was removed. Don't wire against that FFL expecting it to run.
- **Suffix stripping is dedup-critical.** Skipping `_strip_admin_type` re-introduces
  the ~2× county duplication between the TIGER and self-gen boundary sources.
- **County nesting is mandatory.** ~30 states have a "Washington County"; a flat key
  would collide, so counties are always keyed under their state slug.
- **TIGER is US-only.** It's the sub-national poly source specifically because osmfr
  lacks US state polys and the county tree; other countries' subdivisions come from
  osmfr `/polygons/` or self-generated OSM boundaries.
- **pyshp is required** for any TIGER path — it's failed loudly up front, not
  silently skipped.

## Related specs

- [planet-extraction](planet-extraction.md) — the primary consumer; TIGER GeoJSON
  polys are the US state/county boundaries its extractor clips against, and the
  county-suffix normalization is shared with its `boundary_gen`.
- [cache-and-download](cache-and-download.md) — the region keys TIGER polys are
  keyed under (`north-america/us/<state>[/<county>]`) resolve here.
