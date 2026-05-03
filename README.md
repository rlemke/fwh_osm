# osm-geocoder

A standalone [Facetwork](https://github.com/rlemke/facetwork) example package
providing FFL workflows and handlers for working with OpenStreetMap data:

- **Source adapters** — extract GeoJSON features from `.osm.pbf`, PostGIS, or existing GeoJSON files
- **Categorical extractors** — routes, amenities, roads, parks, buildings, boundaries, population, POIs
- **Bulk PostGIS imports** — multi-hour, local-first staged imports with autovacuum management
- **Routing graphs** — GraphHopper, Valhalla, OSRM, and pgRouting
- **Address geocoding** — Nominatim API
- **Visualization** — Folium HTML maps and vector tiles
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

```bash
# In your Facetwork repo:
scripts/seed-examples --include osm-geocoder
scripts/start-runner --example osm-geocoder -- --log-format text
```

`runner.env` overrides (4-hour task / stuck timeouts for long PostGIS
imports) are baked into `osm_geocoder:example` and applied automatically
when the runner starts.

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
│   ├── _lib/                       # shared library for tools
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
