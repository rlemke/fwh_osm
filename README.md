# osm-geocoder

A standalone [Facetwork](https://github.com/rlemke/facetwork) example package for working with
OpenStreetMap data. It is a **composable facet library**: an orthogonal, path-chaining set of
primitives (`CacheRegion → Clip → ExtractCategory → Filter* → Spatial/Transform → Render/Tiles`)
that an LLM can discover (`fw_capabilities`) and compose from a natural-language request. The layers:

- **Source adapters** — extract GeoJSON features from `.osm.pbf`, PostGIS, or existing GeoJSON files
- **Clip** (`osm.Clip`) — subset a region PBF to a bbox/polygon (osmium) for cheap metro-scale queries
- **Categorical extraction** — `ExtractCategory` (cheap, cached, warm-pass) + routes / amenities / roads / parks / buildings / boundaries / population / POIs
- **Filter** (`osm.Filters`) — by OSM type, tag exact / prefix / contains / regex, radius
- **Spatial** (`osm.Spatial`, 9 verbs) — WithinDistance, BeyondDistance, Nearest, SpatialJoin, Buffer, Intersect, Union, Centroid, Simplify (shapely)
- **Transform** (`osm.Transform`) — MergeLayers, Summarize, Dissolve
- **Geocoding** (`osm.geocode`) — forward + reverse via Nominatim
- **Routing** (`osm.Routing.{OSRM,API,Valhalla,GraphHopper,PgRouting}`) — Route, MultiStop, Isochrone, Matrix, Nearest, MapMatch, Trip across five swappable engines (uniform schemas)
- **Network — engine-free approx routing** (`osm.Network`) — BuildNetwork / ApproxRoute / RouteMatrix: pure shapely/networkx graph search over a tiny noded-freeway cache artifact (no daemon, read-once-per-runner, embarrassingly parallel). `+ CityRoutesByPopulation` / `RouteFanout` workflows
- **Vocabulary** (`osm.Vocab`) — natural-language term → OSM `key=value` (e.g. "pharmacy" → `amenity=pharmacy`)
- **Visualization** — Folium/Leaflet HTML maps + vector tiles (`osm.Tiles`, tippecanoe → MBTiles/PMTiles)
- **Bulk PostGIS imports** — multi-hour, local-first staged imports with autovacuum management
- **Routing graph builders** — GraphHopper / Valhalla / OSRM graph + tile construction
- **Voting / Census TIGER** — district boundaries and demographic overlays

Discovered by the Facetwork runner via the `facetwork.domains` entry point
declared in `pyproject.toml`. After `pip install -e .`, Facetwork's
`fw runner start --domain osm-geocoder` and `fw ffl seed`
pick this package up automatically.

## FFL at a glance

The domain is driven from [FFL](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md),
Facetwork's workflow language. A step is `name = Facet(args)`; the cost tiers are
the design — resolve (`pure`) → download the PBF once (`expensive`, idempotent) →
chain `cheap` filters over the extracted GeoJSON:

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache
    use osm.Amenities
    use osm.Vocab
    use osm.Filters

    /** "coffee shop" → amenity=cafe → filtered GeoJSON for a named region. */
    workflow FindByTerm(region: String = "Oregon", term: String = "coffee shop") => (path: String, tag: String) andThen {

        resolved = osm.Region.ResolveRegion(name = $.region)
        cached = osm.cache.Download(region = resolved.region)

        tag = osm.Vocab.ResolveTag(term = $.term)

        all = osm.Amenities.ExtractAmenities(cache = cached.cache, category = "all")

        matched = osm.Filters.FilterByOSMTag(
            input_path = all.result.output_path,
            tag_key = tag.result.osm_key,
            tag_value = tag.result.osm_value)

        yield FindByTerm(
            path = matched.result.output_path,
            tag = tag.result.osm_key ++ "=" ++ tag.result.osm_value)
    }
}
```

```bash
fw ffl run --primary my.ffl --library <the ffl files it uses> \
  --workflow my.osm.FindByTerm --task-list osm
```

📖 **[docs/ffl-examples.md](docs/ffl-examples.md)** — the full example gallery:
the resolve→download→extract chain, extract-once-filter-many, NL→tag lookup,
`foreach` over regions (and the heavy-fan-out caveat), `when` guards on the
expensive download, call-time mixins + `catch`, and wrapping the shipped map
workflows. Every snippet there is compile-checked.

## Feature specifications

Every OSM feature has a spec in [**`docs/`**](docs/README.md) — how it works,
whether/how it **fans out**, what it **filters** and on which OSM **attributes**, the
**external libraries/binaries** it uses, its **facets & workflows**, and its
**cache/output**. Start with the flagship
[**Planet Extraction**](docs/planet-extraction.md) (the self-hosted "Geofabrik") and
the cross-cutting [**fan-out pattern**](docs/fan-out-pattern.md); the full index is
in [`docs/README.md`](docs/README.md).

| Area | Specs |
|------|-------|
| **Flagship & cross-cutting** | [planet-extraction](docs/planet-extraction.md) · [fan-out-pattern](docs/fan-out-pattern.md) |
| **Ingest & sources** | [cache-and-download](docs/cache-and-download.md) · [source-adapters](docs/source-adapters.md) · [shapefiles](docs/shapefiles.md) · [osm-changes](docs/osm-changes.md) · [clip](docs/clip.md) |
| **Filter / transform / vocab** | [filters](docs/filters.md) · [transform](docs/transform.md) · [population](docs/population.md) · [vocab](docs/vocab.md) · [spatial](docs/spatial.md) |
| **Features / POI / buildings** | [amenities](docs/amenities.md) · [buildings](docs/buildings.md) · [poi](docs/poi.md) · [parks](docs/parks.md) · [boundaries](docs/boundaries.md) |
| **Visualization / tiles / roads** | [visualization](docs/visualization.md) · [heatmaps](docs/heatmaps.md) · [tiles](docs/tiles.md) · [roads](docs/roads.md) |
| **Routing** | [routing](docs/routing.md) · [graphhopper](docs/graphhopper.md) · [valhalla](docs/valhalla.md) · [routes](docs/routes.md) · [network](docs/network.md) |
| **Composition & apps** | [emergency-atlas](docs/emergency-atlas.md) · [cities](docs/cities.md) · [voting](docs/voting.md) · [postgis-db](docs/postgis-db.md) · [composed-workflows](docs/composed-workflows.md) · [geocoding](docs/geocoding.md) |

## Install

```bash
git clone https://github.com/rlemke/fwh_osm.git
cd fwh_osm
pip install -e .
```

Or let Facetwork's registry-driven installer clone + install it for you:

```bash
fw install domain osm-geocoder
```

This registers the package under the `facetwork.domains` entry-point group,
making it discoverable by any Facetwork installation in the same environment.

## Run from a Facetwork checkout

All commands below assume your shell is in the Facetwork checkout and the
osm package is installed in the same Python environment that runs
Facetwork (`pip install -e ~/fw_handlers/fwh_osm`).

The package's `runner_env` (4-hour task / stuck timeouts for long PostGIS
imports) is baked into `osm_geocoder:domain` and applied automatically to
any runner started with `--domain osm-geocoder`.

### Cold start: dashboard + runner together

```bash
fw ffl seed --include osm-geocoder           # one-time, seeds FFL
fw runner start --domain osm-geocoder -- --log-format text
```

This brings up the dashboard on `:8080` and a runner that polls for
osm-geocoder tasks.

### Add a runner to an already-running stack

If the Facetwork dashboard is already up and you just want another runner
attached to it (after pulling new osm code, or to scale out):

```bash
fw runner start --domain osm-geocoder --no-dashboard -- --log-format text
```

Internally this runs `python -m facetwork.domains osm-geocoder` against
the same MongoDB the dashboard uses — registering the osm handlers in the
`handlers` collection — and then starts a `RegistryRunner` process. The
new runner appears in the dashboard's `/servers` page within a few
seconds. Multiple runners on the same host coexist; each picks tasks
independently.

### Variants

- **Multiple instances on this host:** `--instances N` spawns N runner
  processes sharing one handler registration.
- **Remote host:** `fw runner start --host h2.example --domain osm-geocoder --no-dashboard`
  (the osm package must be installed on the remote venv too; needs SSH
  reachability and `FW_RUNNER_HOSTS` or repeated `--host` flags).
- **Register handlers without starting a runner:**
  `python -m facetwork.domains osm-geocoder` — handy if a runner is
  already running but its handler set is stale.
- **Drain a runner cleanly:** `fw runner drain` resets in-flight
  tasks to pending so another runner can pick them up.

## Run standalone

```bash
PYTHONPATH=src python agent.py
```

## Layout

```
fwh_osm/
├── pyproject.toml                  # facetwork.domains entry point
├── README.md
├── CLAUDE.md                       # guidance for Claude Code in this repo
├── USER_GUIDE.md                   # human-facing walkthrough
├── docs/                           # per-feature specifications (see docs/README.md)
├── runner.env.example              # informational only (values live in __init__.py)
├── .claude/                        # MCP server config for Claude Code
├── agent-spec/                     # tools-pattern, cache-layout specs
├── agent.py                        # standalone AgentPoller variant
├── scripts/                        # repo-level helper scripts (serve-tiled-map)
├── tests/                          # repo-level integration tests
└── src/osm_geocoder/
    ├── __init__.py                 # exports `domain: DomainPackage`
    ├── tools/                      # CLI scripts (PBF → tiles → HTML)
    │   ├── _osm_tools/             #   shared library for tools
    │   ├── all-extract.sh
    │   ├── all-render-html-maps.sh
    │   └── ...
    ├── handlers/                   # handler subpackages (one per domain)
    │   ├── amenities/
    │   ├── boundaries/
    │   ├── ...
    │   └── voting/
    └── ffl/                        # top-level FFL workflows (geocoder.ffl)
```

Per-domain FFL files live alongside their handlers under
`src/osm_geocoder/handlers/<domain>/ffl/`, matching the Facetwork
domain-pipeline convention (see `agent-spec/tools-pattern.agent-spec.yaml`).

## Required infrastructure

| Service | Purpose |
|---------|---------|
| MongoDB | Facetwork registry + workflow state |
| PostgreSQL/PostGIS | OSM bulk imports, spatial queries |

PostGIS is required for the `osm.Source.PostGIS` adapter; PBF and GeoJSON
adapters work without it. See `USER_GUIDE.md` for end-to-end setup.

## License

Apache 2.0 — see `LICENSE`.
