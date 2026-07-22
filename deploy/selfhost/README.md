# Self-hosted OSM extract server (Strategy A)

Run your own Geofabrik: a **nightly job** that keeps a master planet current and
re-extracts your regions, plus a **static server** that serves the result. Point
`FW_GEOFABRIK_BASE_URL` at it and the existing fwh_osm download path consumes it
unchanged — Geofabrik out of the critical path.

Two launchd agents (macOS):

| Agent | What it does |
|-------|--------------|
| `com.facetwork.osm-extract-server` | KeepAlive `http.server` serving `WWW` on `PORT` |
| `com.facetwork.osm-maintain` | nightly `planet-maintain`: advance master → re-extract into `WWW` |

Tools behind them: [`planet_bootstrap`](../../src/osm_geocoder/tools/planet_bootstrap.py)
(Phase 1 split) and [`planet_maintain`](../../src/osm_geocoder/tools/planet_maintain.py)
(Phase 2 loop).

## Install

```bash
deploy/selfhost/install.sh          # 1st run seeds ~/.facetwork/osm-selfhost/config.env
$EDITOR ~/.facetwork/osm-selfhost/config.env
deploy/selfhost/install.sh          # installs + loads both agents
```

Then provide a **master** and a **regions** spec (see below) and kick a first cycle:

```bash
deploy/selfhost/maintain-wrapper.sh ~/.facetwork/osm-selfhost/config.env
curl -sI http://<host>:<PORT>/<region>-latest.osm.pbf   # served
```

## The master planet

`MASTER` is a PBF **with a replication header** that the nightly job advances in
place. Two ways to get one:

- **Real planet (production):** download `planet-latest.osm.pbf` (~80 GB) from a
  planet mirror (planet.openstreetmap.org — not the banned Geofabrik host); it
  carries a planet replication header. Provision the scratch disk (~300–500 GB
  incl. the node-location index that `smart`/`complete_ways` needs). `regions.json`
  uses real `.poly` boundaries.
- **Stand-in (bring-up / testing):** any small country extract with a replication
  header (e.g. `europe/monaco-latest.osm.pbf`). Exercises the whole pipeline at
  MB scale so you can validate the wiring before committing the planet download.

## regions.json

Same spec as `planet_bootstrap` — one object per region:

```json
[
  {"key": "europe/germany",            "poly": "/path/germany.poly"},
  {"key": "north-america/us/california","poly": "/path/california.poly"},
  {"key": "demo/tile",                  "bbox": [7.409, 43.723, 7.425, 43.752]}
]
```

`.poly` files come from Geofabrik's per-region `.poly` publications or from OSM
admin boundaries. `bbox` tiles are for prototyping / axis-aligned areas.

## Consuming from the fleet

Set `FW_GEOFABRIK_BASE_URL` to `BASE_URL` on consumers. The download path fetches
`<base>/<region>-latest.osm.pbf` and the delta path follows the stamped
`<base>/<region>-updates/` — one base URL, both paths. **Docker runners** don't
resolve `.local` mDNS inside the VM (same caveat as the registry) — point them at
the infra IP or an `afl-*` alias mapped via `extra_hosts`, not `server3.local`.

Strategy A publishes **no per-region diffs**: regions refresh by whole-extract
re-download (the download path revalidates via `Last-Modified`), which avoids the
reference-completeness hazard of per-region diff clipping (that would be Strategy B).

## Operate

```bash
launchctl print gui/$(id -u)/com.facetwork.osm-extract-server   # server status
launchctl print gui/$(id -u)/com.facetwork.osm-maintain          # timer status
launchctl kickstart -k gui/$(id -u)/com.facetwork.osm-maintain   # run maintain now
tail -f ~/.facetwork/osm-selfhost/{server,maintain}.log
```

**Serving at scale:** `http.server` is a fine start for a LAN of runners; front it
with nginx/caddy, or publish `WWW` into MinIO (`s3://afl-cache`) and serve from
there, when you outgrow it.
