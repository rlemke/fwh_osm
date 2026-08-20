# Publishing replication diffs — Phase 2 of the self-hosted split

Phase 1 split the planet into regional extracts and stamped each one with
`osmosis_replication_base_url` pointing at **our** server, so the delta path
would follow us instead of Geofabrik. It created the `<region>-updates/`
directories too.

It never put anything in them. Every `state.txt` held a bare `timestamp=` with
**no `sequenceNumber`**, and not one `.osc.gz` was ever published:

```
$ cat www/north-america-updates/state.txt
timestamp=2026-07-12T23\:59\:57Z
$ find www -name '*.osc.gz' | wc -l
0
```

The indirection was wired to an empty room. Every extract has therefore been a
frozen snapshot — **39 days stale** when this was written — and silently so:
nothing errors, the data is just old. That is what made an unrelated map's data
quietly worse than the live source it was meant to replace.

## What Phase 2 does

For each new day of OSM replication, cut the planet diff down to every region's
polygon and publish the result as that region's own sequenced diff. That is
precisely what Geofabrik does, and precisely what `pbf_update.update_region`
(shipped in Phase 1) already knows how to consume.

```bash
publish-replication.sh --status                 # how far behind is each region
publish-replication.sh --anchor 5051            # ONE-TIME baseline
publish-replication.sh --stamp-extracts \
    --base-url http://server3.local:8088        # ONE-TIME, rewrites each PBF
publish-replication.sh --days 7                 # nightly
```

## Why cutting diffs, and not updating the planet

The obvious alternative is `pyosmium-up-to-date` on the 87 GB planet followed by
a re-split. Measured on this deployment:

| | |
|---|---|
| raw disk read | **32 MB/s** |
| one pass over the 87 GB planet | ~45 min |
| planet update + re-split | hours, per refresh |
| **cutting one day's diff** | 83 MB in, **~27 s of CPU per region**, 344 KB out |

Cutting never reads the planet file at all. The cost scales with the day's
edits, not with the size of the world.

## Publish, do not apply

This writes diffs; it does **not** roll the served extracts forward. That is the
Geofabrik contract, and it is the cheap half — publishing europe's diff takes
seconds, while applying it to `europe-latest.osm.pbf` is a 37 GB read plus
write. Consumers apply when they want fresh data, and because `osmium
apply-changes` accepts many change files in one pass, catching up 39 days costs
a consumer the same single pass as catching up one.

## The two sequences — the trap in this design

Because diffs are published without being applied, **the published head runs
ahead of the served extract by design**. Two different sequence numbers exist
and must never be conflated:

| | meaning | file |
|---|---|---|
| **head** | newest diff available to consumers | `<region>-updates/state.txt` |
| **extract** | what the served `.osm.pbf` actually contains | `<region>-updates/extract.state.txt` |

Stamping the *head* into an extract tells consumers it already holds diffs it
does not, so they start after them and silently skip every edit published so
far. This bit during development: `central-america-latest.osm.pbf` was stamped
5053 while its data was at 5051, which would have lost two days of edits for
every consumer, with no error anywhere. `--anchor` records both while they are
still equal, and `--stamp-extracts` deliberately reads the *extract* one.

## Anchoring

The anchor is one-time and **never inferred**. It must be the last upstream
sequence already contained in the served extract:

```
extract timestamp  2026-07-12T23:59:57Z
upstream seq 5051  closes 2026-07-12T00:00:00Z    <- anchor here
upstream seq 5052  closes 2026-07-13T00:00:00Z
```

The extract holds nearly all of 5052 but not quite. Anchor at **5051**:
re-applying a day is idempotent, whereas anchoring at 5052 leaves a three-second
gap that never closes. When unsure, always go one lower.

Sequence numbers are OSM's own day numbers on purpose — `000/005/090.osc.gz`
here is knowably the same day as upstream's, so there is no private mapping to
debug against.

## Verified end to end

With the tree served over HTTP and **no hints given**, a consumer reads
everything from the extract's own header:

```
discovered from header:
   url      : http://…/central-america-updates
   sequence : 5051
   timestamp: 2026-07-12 00:00:00+00:00

apply_diffs_to_file -> (5053, 5053) in 8.9s
```

and the resulting extract is correctly stamped at 5053 / 2026-07-14.

## Operating it

`--days` is bounded on purpose: a never-published region is arbitrarily far
behind, and an unbounded catch-up would pull tens of GB without anyone choosing
to. Each run leaves a consistent `state.txt` — `state.txt` is written *after*
each diff lands, so an interrupted run never advertises a diff that is not
there. Stopping early is safe; resuming is free.

Ongoing cost is one 83 MB download per day plus ~27 s of CPU per region, so a
nightly `--days 2` keeps eight regions current in a few minutes.

## Rolling the served extracts forward — `--apply`

Publishing does not apply, which is right for consumers but leaves anything
reading these FILES directly — local tag queries, an offline fallback — as old
as the last apply. The extract and the stream are different things, and only
the stream advances nightly.

```bash
publish-replication.sh --apply            # every region
publish-replication.sh --apply --region europe
```

All pending diffs go into ONE `osmium apply-changes` pass: applying day by day
would re-read and re-write the whole extract each time, so europe's 39 days
would be 39 x 37 GB instead of once. Measured ~59 MB/s of extract, so the full
93 GB across eight regions is roughly half an hour.

It **refuses to apply across a gap**. If `state.txt` advertises a sequence whose
diff is missing, applying anyway produces an extract quietly missing a day while
its header asserts it is current — and every consumer then trusts that header.
That is the worst corruption available here, so it errors instead.

After applying, the extract's recorded sequence moves with it. Leaving it behind
would make the next apply redo everything (harmless but wasteful) and would make
`--stamp-extracts` write a stale baseline (not harmless at all).

## Interruption is expected, and safe

Both halves are designed to be killed. During the first catch-up on this
deployment they were, mid-run, and neither left damage:

* **Stamping** replaces the extract only after a *complete* rewrite, so the
  interrupted region (asia, 17 GB) kept its original file and left a
  `.stamping.osm.pbf` temp to delete. A half-written extract served to a
  consumer would be worse than a stale one.
* **Publishing** writes `state.txt` *after* each diff lands, so the run stopped
  at a consistent 5062 for all eight regions and resumed from there for free.
  It never advertises a diff that is not on disk.

Prefer bounded chunks (`--days 14`, one region per stamp) over one long run:
each completes, and nothing is lost when something outside the tool decides the
job has gone on long enough.

## Cost, measured

| step | cost |
|---|---|
| one day, all 8 regions | 83 MB download + ~30 s CPU (one pass) |
| stamping central-america (853 MB) | ~1 min |
| stamping asia (17 GB) | ~20 min |
| stamping europe (37 GB) | ~40 min |
| full 39-day catch-up | ~3.2 GB down, ~20 min CPU |

Measured over one real 14-day chunk — 1.3 GB fetched upstream, and this much
published per region:

| region | 14 days of diffs | full extract |
|---|---:|---:|
| europe | 468 MB | 37 GB |
| asia | 230 MB | 17 GB |
| north-america | 194 MB | 20 GB |
| africa | 70 MB | 8.2 GB |
| south-america | 60 MB | 4.2 GB |
| russia | 37 MB | 4.2 GB |
| oceania | 26 MB | 1.6 GB |
| central-america | 18 MB | 853 MB |

That ratio is the whole argument for the delta path: keeping europe current
costs ~33 MB a day against the 37 GB a re-download would cost — roughly a
thousand to one. Ongoing, a nightly `--days 2` keeps all eight regions current
in a few minutes and well under 100 MB.
