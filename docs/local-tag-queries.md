# Ad-hoc tag queries — asking OSM anything, locally

`osm.query.TagQuery` answers an arbitrary OpenStreetMap tag question from the
extracts already on disk. No Overpass, no API key, no rate limit.

```bash
fw ffl run src/osm_geocoder/handlers/query/ffl/osmquery.ffl \
  --workflow osm.query.example.QueryRegions \
  --inputs '{"regions":["north-america/us/vermont","north-america/us/wyoming"],
             "filter":"nwr/man_made=surveillance","concurrency":2}'
```

Or from the shell, which is how you actually explore a tag:

```bash
export FW_OSM_LOCAL_EXTRACTS=/Volumes/afl_data_local/osm-selfhost/www

tools/query-osm.sh north-america/us/utah --filter 'nwr/amenity=pharmacy'
tools/query-osm.sh europe asia --filter 'nwr/man_made=surveillance'
tools/query-osm.sh north-america --filter 'nwr/man_made=surveillance' \
                                 --where 'surveillance:type=ALPR'
```

`--where` is the AND that osmium cannot express (see below); progress goes to
stderr and `--json` puts machine-readable results on stdout, so it pipes.

## Why it exists

[`pbf_extract`](../src/osm_geocoder/tools/_osm_tools/pbf_extract.py) already
turns a region's PBF into GeoJSON with two osmium passes and a sidecar-validated
cache — but its filter is a **closed enum**: 25 curated categories (water, parks,
healthcare …), each with a hand-written expression.

Every question outside those 25 has had to go to Overpass, and that is where this
fleet keeps meeting somebody else's limits. The tag-quality maps are documented
*"cache-first, do NOT fan out (egress rate-limit)"*. The ALPR map is a single
query because a fan-out would be throttled. The shape of those workflows was
decided by a rate limiter rather than by the question.

Meanwhile this machine holds an 87 GB planet, 8 continent extracts and ~200
country extracts. `TagQuery` keeps the proven machinery and opens the filter.

## The cache key is the question

A category cache is invalidated by a human bumping `filter_version`. Forget, and
stale output is served silently — the failure mode is invisible.

Here the digest of the **normalised expression** is part of the cache path:

```
cache/osm/query/f4144755958c/north-america/us/vermont-latest.geojsonseq
                └── sha256("nwr/man_made=surveillance")[:12]
```

so a changed question is a different entry by construction, and an equivalent one
is the same entry. Terms are sorted and whitespace collapsed before hashing, so
`nwr/amenity=cafe nwr/amenity=bar` and the reverse do not each pay for a scan.
Freshness against the source PBF is the existing sidecar sha256 check.

## What it refuses

A bare object-type term (`nwr`, `n/`) matches *everything*, which on a continent
is a multi-hour rewrite of the whole extract wearing a query's clothes. That is a
mistake rather than a request, so it is refused with the valid forms named.
Anything that is not a `[nwr]/key[=value,…]` term is refused the same way.

## Which sources it can see

Two places, in order:

1. **the download cache** (`cache/osm/pbf/<region>-latest.osm.pbf`) — preferred,
   because its sidecar carries a real content digest and that is what freshness
   is judged on;
2. **`FW_OSM_LOCAL_EXTRACTS`** — colon-separated trees of ready-made extracts.

The second exists because the results of self-hosting do *not* live in the
download cache: the 87 GB planet and the continent extracts are produced locally
and published to the object store. Without it the largest sources on the machine
would be exactly the ones a query could not reach.

```bash
export FW_OSM_LOCAL_EXTRACTS=/Volumes/afl_data_local/osm-selfhost/www
```

Both the nested key (`north-america/us/utah-latest.osm.pbf`) and the flat form
`planet_bootstrap` writes (`europe-latest.osm.pbf` in the root) are found. A
local file has no sidecar, so its identity is synthesised from size and mtime —
weaker than a digest, and deliberately: hashing 87 GB to decide whether to scan
it costs as much as scanning it.

## What it costs

Measured on this hardware, single-threaded, all for `nwr/man_made=surveillance`:

| source | size | matches | scan | rate |
|---|---|---|---|---|
| `north-america/us/district-of-columbia` | 21 MB | 664 | 0.8s | — |
| `central-america` | 853 MB | 744 | 27.7s | 31 MB/s |
| `south-america` | 4.2 GB | 10,737 | 133.3s | 32 MB/s |
| `north-america` | 20 GB | 155,485 | **50 min** | **6.8 MB/s** |

**Do not extrapolate from size alone.** An earlier version of this page did
exactly that — "europe ≈ 20 min, planet ≈ 45–50 min", straight-lined from the
853 MB sample — and north-america alone then took 50 minutes, about five times
the predicted rate. The two small runs agree closely at ~31 MB/s and the big one
does not, and the variable that moved is **matches**, not bytes: the second pass
(`osmium export`) assembles a geometry per matching feature, so a filter that
hits 155k features pays for 155k assemblies on top of the read. Disk contention
with the rest of the fleet on the same volume is a second, unquantified factor.

So: a cheap filter over a continent is minutes, an expensive one is an hour, and
a planet-wide scan of a common tag should be treated as *hours* until somebody
measures it. `limit` on the fan-out is not about politeness — there is nobody to
be polite to — it bounds contention for this machine's disk.

## Verified

Four regions, one question, through a real runner:

```
north-america/us/district-of-columbia    664 features
north-america/us/vermont                  41
north-america/us/wyoming                 103
north-america/greenland                    3
```

all under one digest directory, with the second ask of the same question served
from cache in 0.0s.

## Worked example: the ALPR question, without Overpass

`fwh_save_earth`'s ALPR source documents its own constraint plainly — *"all on
one shared per-IP Overpass rate limit — so this is a SINGLE worldwide cached
query, never a per-region fan-out"*. That is the shape of the workflow being
decided by somebody else's throttle.

The same question, asked locally over `north-america`:

```
surveillance features : 155,485
of which ALPR         : 115,093
top vendors           : flock safety 94,205 · motorola solutions 5,639 ·
                        genetec 2,716 · leonardo 933
```

Note the two-step: `tags-filter` gets `man_made=surveillance` (the superset) and
the `surveillance:type=ALPR` half is applied to the resulting GeoJSON. That is
the AND limitation below, and it is cheap here — one pass over the matches
rather than a second pass over the extract.

## Writing filters

Standard [osmium tags-filter](https://docs.osmcode.org/osmium/latest/osmium-tags-filter.html)
syntax:

| goal | filter |
|---|---|
| one tag | `nwr/man_made=surveillance` |
| several values | `nwr/amenity=pharmacy,doctors,clinic` |
| key present, any value | `nwr/man_made` |
| relations only | `r/boundary=protected_area` |
| different types, different tags | `w/highway=motorway n/amenity=fuel` |

Terms are OR-ed, which is what makes one pass answer a family of questions.
`TagQuery` cannot express AND across tags (`amenity=pharmacy` **and**
`opening_hours=24/7`) — filter the resulting GeoJSON for that.
