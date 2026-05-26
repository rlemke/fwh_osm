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

Discovered by the Facetwork runner via the `facetwork.examples` entry point
declared in `pyproject.toml`. After `pip install -e .`, Facetwork's
`scripts/start-runner --example osm-geocoder` and `scripts/seed-examples`
pick this package up automatically.

## Install

```bash
git clone https://github.com/rlemke/osm.git
cd osm
pip install -e .
```

This registers the package under the `facetwork.examples` entry-point group,
making it discoverable by any Facetwork installation in the same environment.

## Run from a Facetwork checkout

All commands below assume your shell is in the Facetwork checkout and the
osm package is installed in the same Python environment that runs
Facetwork (`pip install -e ~/ffl_handlers/osm`).

The package's `runner_env` (4-hour task / stuck timeouts for long PostGIS
imports) is baked into `osm_geocoder:example` and applied automatically to
any runner started with `--example osm-geocoder`.

### Cold start: dashboard + runner together

```bash
scripts/seed-examples --include osm-geocoder           # one-time, seeds FFL
scripts/start-runner --example osm-geocoder -- --log-format text
```

This brings up the dashboard on `:8080` and a runner that polls for
osm-geocoder tasks.

### Add a runner to an already-running stack

If the Facetwork dashboard is already up and you just want another runner
attached to it (after pulling new osm code, or to scale out):

```bash
scripts/start-runner --example osm-geocoder --no-dashboard -- --log-format text
```

Internally this runs `python -m facetwork.examples osm-geocoder` against
the same MongoDB the dashboard uses — registering the osm handlers in the
`handlers` collection — and then starts a `RegistryRunner` process. The
new runner appears in the dashboard's `/servers` page within a few
seconds. Multiple runners on the same host coexist; each picks tasks
independently.

### Variants

- **Multiple instances on this host:** `--instances N` spawns N runner
  processes sharing one handler registration.
- **Remote host:** `scripts/start-runner --host h2.example --example osm-geocoder --no-dashboard`
  (the osm package must be installed on the remote venv too; needs SSH
  reachability and `AFL_RUNNER_HOSTS` or repeated `--host` flags).
- **Register handlers without starting a runner:**
  `python -m facetwork.examples osm-geocoder` — handy if a runner is
  already running but its handler set is stale.
- **Drain a runner cleanly:** `scripts/drain-runners` resets in-flight
  tasks to pending so another runner can pick them up.

## Run standalone

```bash
PYTHONPATH=src python agent.py
```

## Layout

```
osm/
├── pyproject.toml                  # facetwork.examples entry point
├── README.md
├── CLAUDE.md                       # guidance for Claude Code in this repo
├── USER_GUIDE.md                   # human-facing walkthrough
├── runner.env.example              # informational only (values live in __init__.py)
├── .claude/                        # MCP server config for Claude Code
├── agent-spec/                     # tools-pattern, cache-layout specs
├── agent.py                        # standalone AgentPoller variant
├── tools/                          # repo-level CLI scripts (PBF → tiles → HTML)
│   ├── _osm_tools/                       # shared library for tools
│   ├── all-extract.sh
│   ├── all-render-html-maps.sh
│   └── ...
├── tests/                          # repo-level integration tests
└── src/osm_geocoder/
    ├── __init__.py                 # exports `example: ExamplePackage`
    ├── handlers/                   # 23 handler subpackages
    │   ├── amenities/
    │   ├── boundaries/
    │   ├── ...
    │   └── voting/
    └── ffl/                        # top-level FFL workflows
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
