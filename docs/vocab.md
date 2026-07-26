# Vocab — NL term → OSM tag

**Namespace(s):** `osm.Vocab` ·
**FFL:** `src/osm_geocoder/handlers/vocab/ffl/osmvocab.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/vocab/vocab_handlers.py` ·
**Tools:** `tools/_osm_tools/vocab.py` (the ontology + resolver)

## Overview

`osm.Vocab` is the **semantic half of the discovery layer** (the facet capability
index is the other half). It resolves a natural-language term — "gas station",
"grocery store", "freeway" — to the OSM `(key, value)` tag it denotes
(`amenity=fuel`, `shop=supermarket`, `highway=motorway`), **deterministically** and
in-process. That turns an NL request into a runnable pipeline without a human
memorizing tags: `ResolveTag("pharmacy") → amenity=pharmacy`, then feed that
key/value into `ExtractCategory` + `FilterGeoJSONByOSMType`.

It is a small, curated ontology, not an exhaustive one — an unknown term resolves
to **confidence 0 with an empty key/value**, an honest "no known tag", rather than
a wrong guess.

## How it works

Both facets are pure in-process lookups against a hand-curated ontology
(`_osm_tools.vocab._ONTOLOGY`), a list of `(key, value, [synonyms])` rows. At import
the table is normalized (`_norm`: lowercase, collapse `_`/`-`/whitespace to single
spaces) into per-entry matchable term sets (`value` ∪ synonyms).

- **`ResolveTag(term, key="")`** — `vocab.resolve`: normalizes the term, then scores
  it against every entry (optionally constrained to one `key`) on a **tiered
  match**:
  - **1.0** — exact match of the canonical value
  - **0.9** — exact match of a synonym
  - **0.65** — token-subset (the term's tokens are a subset of an entry term's tokens)
  - **0.5** — substring either direction
  - **0** — no match (dropped)

  Matches are de-duplicated per `(key, value)` keeping the best confidence, and
  returned sorted by confidence desc. The handler returns the best match as
  `TagResolution` (`osm_key`, `osm_value`, `confidence`, `matched_term`) with the
  remaining candidates JSON-encoded into `alternatives`.
- **`ListTagValues(key)`** — `vocab.list_values`: every known value for a tag key
  (e.g. all `amenity` values the vocabulary covers), returned as a JSON array plus
  a count — discovery of what a key can hold.

`Json`-typed returns (`alternatives`, `values`) are emitted as **JSON strings**,
the same convention `CombinedScan` uses.

## Fan-out

Single-task, no fan-out — each call is a synchronous dictionary lookup over an
in-memory table. There is no input file, no PBF, no cache; fan-out would be
meaningless. Composition happens *after* resolution: the resolved key/value is
threaded into the extract/filter facets that do fan out.

## Filtering & attributes

Does no data filtering — it *produces* the tag other facets filter on. The
ontology spans (non-exhaustively):

- **`amenity`** — food/drink (`restaurant`, `fast_food`, `cafe`, `bar`, `pub`,
  `ice_cream`), health (`pharmacy`, `hospital`, `clinic`, `doctors`, `dentist`,
  `veterinary`), education (`school`, `university`, `college`, `kindergarten`,
  `library`), finance (`bank`, `atm`, `bureau_de_change`), transport/fuel
  (`fuel`, `charging_station`, `parking`, `bicycle_parking`, `taxi`), civic
  (`police`, `fire_station`, `post_office`, `townhall`, `place_of_worship`,
  `toilets`, `drinking_water`), leisure (`cinema`, `theatre`, `nightclub`).
- **`shop`** — `supermarket`, `convenience`, `bakery`, … ("grocery store" →
  `shop=supermarket`, "corner store"/"bodega" → `shop=convenience`).
- **`highway`** — `motorway` ("freeway"/"interstate"/"expressway"), `trunk`,
  `primary`, `secondary`, `tertiary`, `residential`, `service`, `footway`
  ("sidewalk"/"footpath"), …

Each row carries the everyday synonyms a requester is likely to type, which is what
lets "drive thru" → `amenity=fast_food` or "ev charger" → `amenity=charging_station`.

## External libraries / binaries

- **stdlib only** — `re` (normalization), `json` (encode alternatives/values). No
  osmium, no shapely, no network, no ML model. The whole namespace is a curated
  Python table plus a scoring function.

## Facets & workflows

`osm.Vocab` (`osmvocab.ffl`) — both `event`, both `with Effect(kind="pure")`
`with Cost(tier="free")`:

| Facet | Kind | Purpose |
|---|---|---|
| `ResolveTag(term, key="")` | event | NL term → best `(osm_key, osm_value)` + confidence + ranked `alternatives` |
| `ListTagValues(key)` | event | All known values for a tag key, as a JSON array + count |

Returns: `ResolveTag → TagResolution` (`osm_key`, `osm_value`, `confidence`,
`matched_term`, `alternatives: Json`); `ListTagValues → (values: Json, count: Long)`.

## Cache / output

No cache, no file output — the facets return their result inline in the step
payload. `Cost(tier="free")` reflects that there is no I/O or engine cost. The only
"output" is the resolved tag/values in the returned struct.

## Gotchas & notes

- **Curated, not exhaustive.** A term outside the ontology returns confidence 0 and
  empty key/value (the handler surfaces it as a `warning` step log). Callers should
  branch on `confidence == 0` / empty key rather than trust every resolution.
- **`term` is required.** An empty `term` (`ResolveTag`) or empty `key`
  (`ListTagValues`) raises `ValueError`.
- **`alternatives`/`values` are JSON strings, not native lists** — a consumer must
  `json.loads` them (the `Json` FFL type convention).
- **Constrain with `key` to disambiguate.** A term that matches under several keys
  returns them all ranked; pass `key="amenity"` (etc.) when the caller already knows
  the intended dimension.

## Related specs

- [filters](filters.md) — `FilterGeoJSONByOSMType` / `FilterByOSMTag` consume the
  resolved `(key, value)`.
- [population](population.md) — place-type resolution is the population-specific
  analogue of this general tag ontology.
- [source-adapters](source-adapters.md) — `ExtractCategory` is the extraction step
  the resolved tag feeds into to build a runnable NL→pipeline.
