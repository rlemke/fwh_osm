"""Inventory the self-hosted OSM region extracts, and the Overpass alternative.

Answers, for every region we are supposed to serve: is it THERE, how CURRENT is
it, how BIG is it, and (opt-in) how many features does it hold. Then the same
currency question for Overpass, so the two sources can be compared on the one
axis that actually differs.

Three things this module knows that are easy to get wrong:

⚠️ **Currency is nearly free; counts are not.** ``osmium fileinfo`` reads only the
header: 0.26 s on a 1.7 GB file. ``fileinfo -e`` (extended) reads the ENTIRE file
to count features - measured 710 s on a 40 GB extract, and a full-scan of the
100 GB set once cost ~52 minutes for two log numbers. So the default survey is
header-only, and counts are opt-in per call. Never make the expensive tier the
default "just to have the numbers".

⚠️ **A remote header costs 64 KB, not the whole file.** The tree serves
``Accept-Ranges: bytes``, and a PBF's replication header lives in the first blob,
so a Range request for the first 64 KB is enough for osmium to report the
sequence number and replication timestamp of a 40 GB extract. Verified against
europe-latest.osm.pbf (40.5 GB) - the whole fleet's currency can be surveyed in
seconds without downloading anything.

⚠️ **There are TWO extract stores and they are NOT interchangeable.** The HTTP
tree serves the 8 CONTINENTS only; the object store additionally holds ~3,877
country/state/county sub-regions that county-atlas consumes. Reporting one as if
it were the other is how "the extracts are fine" gets said about the wrong set,
so each store is surveyed separately and labelled.

Overpass "completeness" is reported as what can actually be established from
outside: whether each mirror answers, its DATA timestamp (``/api/timestamp``),
how far behind wall-clock that is, and its advertised rate limit and free slots
(``/api/status``). It is deliberately NOT a claim about whether Overpass holds
every object - that is not observable from a client.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("osm.extract_inventory")

# The 8 continents the self-hosted split publishes. Overridable so a deployment
# that serves a different set does not have to edit code.
DEFAULT_REGIONS = (
    "africa", "asia", "australia-oceania", "central-america",
    "europe", "north-america", "russia", "south-america",
)
# The tree names these files <region>-latest.osm.pbf, but its region names need
# not match ours; keep the exceptions as an explicit mapping.
TREE_NAME_OVERRIDES = {"australia-oceania": "oceania"}

DEFAULT_TREE_BASE = "http://server3.local:8088"
DEFAULT_BUCKET = "osm-extracts"
DEFAULT_OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

HEADER_PROBE_BYTES = 64 * 1024
_TIMEOUT = 30
_UA = "facetwork-osm-inventory/1.0 (+https://github.com/rlemke/facetwork)"


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def regions() -> list[str]:
    raw = os.environ.get("FW_OSM_INVENTORY_REGIONS", "").strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    return list(DEFAULT_REGIONS)


def tree_base() -> str:
    return (os.environ.get("FW_OSM_EXTRACT_BASE", "").strip()
            or DEFAULT_TREE_BASE).rstrip("/")


def overpass_endpoints() -> list[str]:
    raw = os.environ.get("FW_OVERPASS_ENDPOINTS", "").strip()
    if raw:
        return [e.strip() for e in raw.split(",") if e.strip()]
    return list(DEFAULT_OVERPASS)


def tree_filename(region: str) -> str:
    return f"{TREE_NAME_OVERRIDES.get(region, region)}-latest.osm.pbf"


# --------------------------------------------------------------------------- #
# header probing
# --------------------------------------------------------------------------- #
def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _fileinfo(path: str, *, extended: bool = False) -> dict[str, Any]:
    cmd = ["osmium", "fileinfo", "-j"] + (["-e"] if extended else []) + [path]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def probe_local(path: str, *, count_features: bool = False) -> dict[str, Any]:
    """Header (and optionally feature counts) for an extract on disk."""
    if not os.path.exists(path):
        return {"present": False, "path": path}
    info = _fileinfo(path, extended=count_features)
    opt = (info.get("header") or {}).get("option") or {}
    ts = opt.get("osmosis_replication_timestamp")
    rec: dict[str, Any] = {
        "present": True,
        "path": path,
        "size_bytes": (info.get("file") or {}).get("size"),
        "replication_timestamp": ts,
        "replication_sequence": opt.get("osmosis_replication_sequence_number"),
        "replication_base_url": opt.get("osmosis_replication_base_url"),
        "generator": opt.get("generator"),
        "age_hours": _age_hours(ts),
    }
    if count_features:
        d = info.get("data") or {}
        rec.update(node_count=d.get("count", {}).get("nodes"),
                   way_count=d.get("count", {}).get("ways"),
                   relation_count=d.get("count", {}).get("relations"))
    return rec


def probe_remote(url: str) -> dict[str, Any]:
    """Header of a remote extract using a 64 KB Range request.

    Costs 64 KB regardless of the file's size - see the module docstring. Falls
    back to reporting size-only when the server ignores Range (the header is then
    simply unavailable rather than triggering a multi-GB download).
    """
    rec: dict[str, Any] = {"present": False, "url": url}
    try:
        head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(head, timeout=_TIMEOUT) as r:
            rec["present"] = True
            rec["size_bytes"] = int(r.headers.get("Content-Length") or 0) or None
            rec["accepts_ranges"] = (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
            rec["last_modified"] = r.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        rec["error"] = f"HTTP {exc.code}"
        return rec
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}"
        return rec

    if not rec.get("accepts_ranges"):
        rec["note"] = "server ignores Range; header not read (a full download was NOT attempted)"
        return rec
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Range": f"bytes=0-{HEADER_PROBE_BYTES - 1}"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            chunk = r.read(HEADER_PROBE_BYTES)
        with tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False) as fh:
            fh.write(chunk)
            tmp = fh.name
        try:
            info = _fileinfo(tmp)          # header only: the truncation is fine
            opt = (info.get("header") or {}).get("option") or {}
            ts = opt.get("osmosis_replication_timestamp")
            rec.update(replication_timestamp=ts,
                       replication_sequence=opt.get("osmosis_replication_sequence_number"),
                       replication_base_url=opt.get("osmosis_replication_base_url"),
                       generator=opt.get("generator"),
                       age_hours=_age_hours(ts))
        finally:
            os.unlink(tmp)
    except Exception as exc:  # noqa: BLE001 - a header we cannot read is reported, not fatal
        rec["header_error"] = f"{type(exc).__name__}"
    return rec


# --------------------------------------------------------------------------- #
# the two stores
# --------------------------------------------------------------------------- #
def survey_tree(region_list: list[str] | None = None) -> dict[str, Any]:
    """The HTTP tree - CONTINENTS ONLY (see the docstring's second warning)."""
    base = tree_base()
    out = {"store": "http-tree", "base": base, "scope": "continents only", "regions": {}}
    for region in (region_list or regions()):
        out["regions"][region] = probe_remote(f"{base}/{tree_filename(region)}")
    return out


def survey_object_store(region_list: list[str] | None = None, *,
                        bucket: str | None = None) -> dict[str, Any]:
    """The object store - continents PLUS ~3,877 sub-regions.

    Only the continent objects are probed by name; the sub-region tier is
    COUNTED, not enumerated, because listing thousands of objects to render a
    status page is a cost with no reader.
    """
    bucket = bucket or os.environ.get("FW_OSM_EXTRACT_BUCKET", "") or DEFAULT_BUCKET
    out: dict[str, Any] = {"store": "object-store", "bucket": bucket,
                           "scope": "continents + sub-regions", "regions": {}}
    try:
        from _osm_tools import storage as _st  # type: ignore
        backend = _st.get_storage()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"object store unavailable: {type(exc).__name__}: {exc}"
        return out
    for region in (region_list or regions()):
        key = f"s3://{bucket}/{tree_filename(region)}"
        try:
            out["regions"][region] = {"present": backend.exists(key), "path": key}
        except Exception as exc:  # noqa: BLE001
            out["regions"][region] = {"present": False, "path": key,
                                      "error": type(exc).__name__}
    return out



# --------------------------------------------------------------------------- #
# sub-regions (countries / states / counties)
# --------------------------------------------------------------------------- #
# Key depth IS the administrative tier: the split writes
# <continent>/<country>/<state>/<county>-latest.osm.pbf.
DEPTH_TIERS = {0: "continent", 1: "country", 2: "state-or-province", 3: "county-or-district"}


def _s3_client(endpoint: str | None = None):
    """The runtime's single S3 client construction.

    ⚠️ Used rather than a local boto3.client() so there is ONE credential chain
    and one MinIO config (s3v4 + path-style) in the codebase. This module still
    needs the raw client — a ranged GET reads a multi-GB PBF's replication
    header from its first 64 KB, and multipart transfer publishes planet
    extracts — neither of which the StorageBackend interface covers.

    ⚠️ The localhost/minioadmin defaults are passed EXPLICITLY, preserving this
    module's historical behaviour exactly. A hardcoded credential default is a
    smell — it silently targets a local store when the env is unset instead of
    failing — but removing it changes behaviour for the live planet pipeline and
    deserves its own deliberate step.
    """
    try:
        from facetwork.runtime.storage import s3_client
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("sub-region survey needs boto3 (pip install '.[s3]')") from exc
    return s3_client(
        endpoint or os.environ.get("FW_S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("FW_S3_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("FW_S3_SECRET_KEY", "minioadmin"),
    )


def _probe_s3_header(client, bucket: str, key: str) -> dict[str, Any]:
    """Replication header of one object via a 64 KB ranged GET."""
    rec: dict[str, Any] = {}
    try:
        body = client.get_object(Bucket=bucket, Key=key,
                                 Range=f"bytes=0-{HEADER_PROBE_BYTES - 1}")["Body"].read()
        with tempfile.NamedTemporaryFile(suffix=".osm.pbf", delete=False) as fh:
            fh.write(body)
            tmp = fh.name
        try:
            opt = (_fileinfo(tmp).get("header") or {}).get("option") or {}
        finally:
            os.unlink(tmp)
        ts = opt.get("osmosis_replication_timestamp")
        rec.update(replication_timestamp=ts,
                   replication_sequence=opt.get("osmosis_replication_sequence_number"),
                   replication_base_url=opt.get("osmosis_replication_base_url"),
                   generator=opt.get("generator"),
                   age_hours=_age_hours(ts),
                   has_replication_timestamp=bool(ts))
    except Exception as exc:  # noqa: BLE001
        rec["header_error"] = type(exc).__name__
        rec["has_replication_timestamp"] = False
    return rec


def survey_subregions(*, bucket: str | None = None, sample_per_tier: int = 3,
                      stale_after_days: float = 14.0) -> dict[str, Any]:
    """Inventory the country/state/county extracts in the object store.

    ⚠️ **This tier has no data vintage.** Only the continents carry
    ``osmosis_replication_timestamp``; the sub-regions are cut from them with
    ``osmium extract``, which does not propagate it. They keep an
    ``osmosis_replication_base_url`` - a promise of an update stream - with no
    sequence number saying where to start, so nothing can apply those diffs and
    nothing can state how old the data is. Verified by downloading whole small
    objects, not just header probes, so this is the file's content and not a
    truncation artifact.

    The consequence for this report: sub-region currency is measured from the
    object's LAST-MODIFIED time, which is a WEAKER signal than a replication
    timestamp - it says when the file was written, not how old the data inside
    it is. Every field derived from it is labelled ``mtime_`` so the two can
    never be read as the same measurement.

    Listing is one paginated call for the whole bucket (cheap). Headers are
    SAMPLED per tier, because probing all ~3,900 would be that many ranged GETs
    to establish something the sample already establishes.
    """
    bucket = bucket or os.environ.get("FW_OSM_EXTRACT_BUCKET", "") or DEFAULT_BUCKET
    out: dict[str, Any] = {"store": "object-store", "bucket": bucket,
                           "sample_per_tier": sample_per_tier,
                           "stale_after_days": stale_after_days, "tiers": {}}
    try:
        client = _s3_client()
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        objects = [o for page in pages for o in page.get("Contents", [])
                   if o["Key"].endswith(".osm.pbf")]
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    now = datetime.now(UTC)
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for o in objects:
        tier = DEPTH_TIERS.get(o["Key"].count("/"), "deeper")
        by_tier.setdefault(tier, []).append(o)

    total_stale = 0
    for tier, objs in by_tier.items():
        ages = [(now - o["LastModified"]).total_seconds() / 86400.0 for o in objs]
        stale = [a for a in ages if a > stale_after_days]
        total_stale += len(stale)
        sample = objs[:: max(1, len(objs) // max(1, sample_per_tier))][:sample_per_tier]
        probes = []
        for o in sample:
            rec = _probe_s3_header(client, bucket, o["Key"])
            rec["key"] = o["Key"]
            probes.append(rec)
        with_ts = sum(1 for p in probes if p.get("has_replication_timestamp"))
        out["tiers"][tier] = {
            "count": len(objs),
            "bytes": sum(o["Size"] for o in objs),
            "mtime_oldest_days": max(ages) if ages else None,
            "mtime_newest_days": min(ages) if ages else None,
            "mtime_stale_count": len(stale),
            "sampled": len(probes),
            "sampled_with_replication_timestamp": with_ts,
            "samples": probes,
        }
    out["total_objects"] = len(objects)
    out["total_bytes"] = sum(o["Size"] for o in objects)
    out["mtime_stale_count"] = total_stale
    # A tier whose samples carry no replication timestamp cannot be aged from
    # its content at all. Surfaced as a finding, not buried in the samples.
    out["tiers_without_data_vintage"] = sorted(
        t for t, v in out["tiers"].items()
        if v["sampled"] and not v["sampled_with_replication_timestamp"])
    return out


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #
def _get_text(url: str, timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _status_base(interpreter_url: str) -> str:
    return interpreter_url.rsplit("/", 1)[0]


def overpass_state(endpoints: list[str] | None = None) -> dict[str, Any]:
    """Reachability, DATA currency and capacity for each Overpass mirror.

    "Completeness" is reported as what a client can actually establish: does it
    answer, how far behind wall-clock is its data, and how much quota does it
    offer. Whether it holds every object is not observable from outside and is
    not claimed here.
    """
    out: dict[str, Any] = {"mirrors": [], "checked_at": datetime.now(UTC).isoformat()}
    for ep in (endpoints or overpass_endpoints()):
        base = _status_base(ep)
        # Two INDEPENDENT probes. Collapsing them into one reachable flag made a
        # mirror whose /status answered but whose /timestamp did not read as
        # "reachable=True, error=URLError" - true, useless, and confusing.
        rec: dict[str, Any] = {"endpoint": ep, "timestamp_ok": False, "status_ok": False}
        try:
            ts = _get_text(f"{base}/timestamp").strip()
            rec["data_timestamp"] = ts
            rec["data_lag_hours"] = _age_hours(ts)
            rec["timestamp_ok"] = True
        except Exception as exc:  # noqa: BLE001
            rec["timestamp_error"] = f"{type(exc).__name__}"
        try:
            status = _get_text(f"{base}/status")
            for line in status.splitlines():
                low = line.lower()
                if low.startswith("rate limit"):
                    rec["rate_limit"] = line.split(":", 1)[1].strip()
                elif "slots available now" in low:
                    rec["slots_available"] = line.split()[0]
            rec["status_ok"] = True
        except Exception as exc:  # noqa: BLE001
            rec["status_error"] = f"{type(exc).__name__}"
        # Usable = it can actually answer a query, which is what a caller cares
        # about; a mirror serving /status but nothing else is not usable.
        rec["usable"] = bool(rec["timestamp_ok"] and rec["status_ok"])
        out["mirrors"].append(rec)
    live = [m for m in out["mirrors"] if m.get("usable")]
    out["usable_count"] = len(live)
    out["total_count"] = len(out["mirrors"])
    return out


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def build_report(*, count_features: bool = False, local_dir: str = "",
                 include_object_store: bool = True,
                 include_overpass: bool = True,
                 include_subregions: bool = False,
                 subregion_stale_days: float = 14.0,
                 stale_after_hours: float = 48.0) -> dict[str, Any]:
    """The whole picture: both stores, Overpass, and a verdict.

    ``count_features`` opts into the EXPENSIVE tier and only applies to extracts
    present on local disk - counting a remote file would mean downloading it,
    which is never done implicitly.
    """
    region_list = regions()
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "regions_expected": region_list,
        "stale_after_hours": stale_after_hours,
        "tree": survey_tree(region_list),
    }
    if include_object_store:
        report["object_store"] = survey_object_store(region_list)
    # Off when a caller has its own ProbeOverpass step: probing here as well
    # meant two probes at two different moments, and the report then disagreed
    # with itself ("2/3 usable" beside "1/3 usable" for the same run).
    if include_subregions:
        report["subregions"] = survey_subregions(stale_after_days=subregion_stale_days)
    report["overpass"] = overpass_state() if include_overpass else {
        "mirrors": [], "usable_count": 0, "total_count": 0, "not_probed": True}

    if local_dir:
        local: dict[str, Any] = {}
        for region in region_list:
            p = os.path.join(local_dir, tree_filename(region))
            local[region] = probe_local(p, count_features=count_features)
        report["local"] = {"store": "local-disk", "dir": local_dir,
                           "counted_features": count_features, "regions": local}

    tree_regions = report["tree"]["regions"]
    missing = sorted(r for r, v in tree_regions.items() if not v.get("present"))
    ages = {r: v.get("age_hours") for r, v in tree_regions.items()
            if v.get("age_hours") is not None}
    stale = sorted(r for r, a in ages.items() if a > stale_after_hours)
    unknown_age = sorted(r for r, v in tree_regions.items()
                         if v.get("present") and v.get("age_hours") is None)
    report["summary"] = {
        "expected": len(region_list),
        "present": sum(1 for v in tree_regions.values() if v.get("present")),
        "missing": missing,
        "stale": stale,
        "unknown_age": unknown_age,
        "oldest_age_hours": max(ages.values()) if ages else None,
        "newest_age_hours": min(ages.values()) if ages else None,
        "overpass_usable": ("not probed" if report["overpass"].get("not_probed")
                            else f"{report['overpass']['usable_count']}"
                                 f"/{report['overpass']['total_count']}"),
    }
    sub = report.get("subregions") or {}
    if sub and not sub.get("error"):
        report["summary"]["subregion_objects"] = sub.get("total_objects")
        report["summary"]["subregion_stale"] = sub.get("mtime_stale_count")
        report["summary"]["subregion_tiers_without_vintage"] = sub.get(
            "tiers_without_data_vintage", [])
        # Deliberately a SEPARATE verdict, not folded into the headline. The
        # sub-region tier is refreshed on its own schedule (fw svc osm-admin-regen)
        # and is currently stale by design-drift, so folding it in would leave the
        # top-level status permanently red - the exact way an alarm gets ignored.
        report["subregion_status"] = "stale" if sub.get("mtime_stale_count") else "ok"

    # Exit-code semantics mirror `fw maint dead-letters` / `osm-watchdog`:
    # 0 healthy, 1 a real problem, 2 could-not-verify. Alarming when merely
    # unable to reach the tree would train the reader to ignore the alarm.
    if missing or stale:
        report["status"] = "problem"
    elif not tree_regions or all(v.get("error") for v in tree_regions.values()):
        report["status"] = "unverified"
    else:
        report["status"] = "ok"
    return report


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
_STATUS_COLOR = {"ok": "#2e7d32", "problem": "#c62828", "unverified": "#f9a825",
                 "partial": "#f9a825"}


def _row(region: str, v: dict[str, Any], stale_h: float) -> str:
    if not v.get("present"):
        state, colour = "MISSING", "#c62828"
    else:
        age = v.get("age_hours")
        if age is None:
            state, colour = "no header", "#f9a825"
        elif age > stale_h:
            state, colour = f"STALE {age:.1f}h", "#c62828"
        else:
            state, colour = f"{age:.1f}h", "#2e7d32"
    size = f"{v['size_bytes'] / 1e9:.1f} GB" if v.get("size_bytes") else "-"
    counts = ""
    for key, label in (("node_count", "nodes"), ("way_count", "ways"),
                       ("relation_count", "relations")):
        if isinstance(v.get(key), int) and v[key] >= 0:
            counts += f"<td>{v[key]:,}</td>"
        else:
            counts += "<td class='dim'>-</td>"
    return (f"<tr><td>{region}</td><td style='color:{colour};font-weight:600'>{state}</td>"
            f"<td>{size}</td><td>{v.get('replication_sequence') or '-'}</td>"
            f"<td class='dim'>{v.get('replication_timestamp') or '-'}</td>{counts}</tr>")


def _subregion_html(sub: dict[str, Any] | None) -> str:
    """The country/state/county tier - rendered ONLY from mtime, and said so."""
    if not sub:
        return ("<h2>Sub-regions</h2><p class='dim'>Not surveyed "
                "(pass include_subregions).</p>")
    if sub.get("error"):
        return f"<h2>Sub-regions</h2><p class='dim'>Could not survey: {sub['error']}</p>"
    rows = "".join(
        f"<tr><td>{tier}</td><td>{v['count']:,}</td>"
        f"<td>{v['bytes'] / 1e9:.1f} GB</td>"
        f"<td>{v['mtime_oldest_days']:.1f} d</td>"
        f"<td style='color:{'#c62828' if v['mtime_stale_count'] else '#2e7d32'};font-weight:600'>"
        f"{v['mtime_stale_count']:,}</td>"
        f"<td>{v['sampled_with_replication_timestamp']}/{v['sampled']}</td></tr>"
        for tier, v in sorted(sub.get("tiers", {}).items(), key=lambda kv: -kv[1]["count"]))
    missing = sub.get("tiers_without_data_vintage") or []
    warn = ""
    if missing:
        warn = (f"<div class='note'><b>No data vintage for: {', '.join(missing)}.</b> "
                "Only the continents carry <code>osmosis_replication_timestamp</code>; "
                "sub-regions are cut from them with <code>osmium extract</code>, which does "
                "not propagate it. They keep an <code>osmosis_replication_base_url</code> - "
                "a promise of an update stream - with NO sequence number saying where to "
                "start, so nothing can apply those diffs and nothing can state how old the "
                "data is. Ages below are therefore the object's LAST-MODIFIED time: when the "
                "file was written, not how old the data inside it is.</div>")
    return f"""<h2>Sub-regions <span class="dim">({sub.get('total_objects', 0):,} objects,
 {sub.get('total_bytes', 0) / 1e9:.1f} GB in {sub.get('bucket', '?')})</span></h2>
{warn}
<table><tr><th>tier</th><th>count</th><th>size</th><th>oldest (mtime)</th>
<th>stale &gt;{sub.get('stale_after_days', 14):g}d</th><th>sampled w/ vintage</th></tr>
{rows}</table>"""


def render_report(report: dict[str, Any], *, dest: str = "") -> tuple[str, str]:
    """Write the survey as HTML + JSON. Returns (html_path, json_path).

    The HTML states the COST TIER it was produced at, because a reader cannot
    otherwise tell an absent feature count from a zero one - the header-only
    survey never measures counts, and silently showing blanks would read as
    "this extract is empty".
    """
    dest = dest or os.environ.get("FW_OSM_INVENTORY_DIR", "") or tempfile.mkdtemp(
        prefix="osm-inventory-")
    os.makedirs(dest, exist_ok=True)
    json_path = os.path.join(dest, "osm-extract-state.json")
    html_path = os.path.join(dest, "osm-extract-state.html")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)

    s = report.get("summary", {})
    status = report.get("status", "unverified")
    stale_h = float(report.get("stale_after_hours", 48.0))
    counted = bool((report.get("local") or {}).get("counted_features"))
    tree = report.get("tree", {})
    local = (report.get("local") or {}).get("regions", {})

    rows = []
    for region in sorted(tree.get("regions", {})):
        v = dict(tree["regions"][region])
        v.update({k: val for k, val in (local.get(region) or {}).items()
                  if k in ("node_count", "way_count", "relation_count")})
        rows.append(_row(region, v, stale_h))

    mirrors = (report.get("overpass") or {}).get("mirrors", [])
    mrows = "".join(
        f"<tr><td>{m.get('endpoint', '?')}</td>"
        f"<td style='color:{'#2e7d32' if m.get('usable') else '#c62828'};font-weight:600'>"
        f"{'usable' if m.get('usable') else 'down'}</td>"
        f"<td>{(f'{m[chr(100)+chr(97)+chr(116)+chr(97)+chr(95)+chr(108)+chr(97)+chr(103)+chr(95)+chr(104)+chr(111)+chr(117)+chr(114)+chr(115)]:.2f} h') if isinstance(m.get('data_lag_hours'), (int, float)) else '-'}</td>"
        f"<td>{m.get('slots_available', '-')}</td>"
        f"<td class='dim'>{m.get('timestamp_error') or m.get('status_error') or ''}</td></tr>"
        for m in mirrors)

    html = f"""<!doctype html><meta charset="utf-8">
<title>OSM extract state</title>
<style>
 body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:2rem;color:#222}}
 h1{{margin:0 0 .2rem}} table{{border-collapse:collapse;margin:1rem 0;width:100%}}
 th,td{{padding:.35rem .6rem;border-bottom:1px solid #e3e3e3;text-align:left}}
 th{{background:#fafafa;font-weight:600}} .dim{{color:#888}}
 .badge{{display:inline-block;padding:.15rem .6rem;border-radius:3px;color:#fff;font-weight:600}}
 .note{{background:#fffbe6;border-left:3px solid #f9a825;padding:.6rem .9rem;margin:1rem 0}}
</style>
<h1>OSM extract state
  <span class="badge" style="background:{_STATUS_COLOR.get(status, '#888')}">{status}</span></h1>
<p class="dim">generated {report.get('generated_at', '?')} &middot;
   stale threshold {stale_h:g} h &middot;
   {s.get('present', 0)}/{s.get('expected', 0)} regions present &middot;
   Overpass {s.get('overpass_usable', '?')} usable</p>

<div class="note">
 <b>Cost tier: {'feature counts (whole-file scan)' if counted else 'header only'}.</b>
 {'' if counted else
  'Feature counts were NOT measured - a header probe never reads them, so the count '
  'columns are blank rather than zero. Counting reads every byte of every extract '
  '(measured 710 s on one 40 GB file); run the deep report deliberately.'}
 Remote headers cost a 64 KB Range request each, so this survey downloaded no extracts.
</div>

<h2>Region extracts &mdash; {tree.get('store', '?')} <span class="dim">({tree.get('scope', '')})</span></h2>
<p class="dim">{tree.get('base', '')}</p>
<table><tr><th>region</th><th>currency</th><th>size</th><th>seq</th><th>replication timestamp</th>
<th>nodes</th><th>ways</th><th>relations</th></tr>
{''.join(rows)}</table>

{_subregion_html(report.get("subregions"))}
<h2>Overpass mirrors</h2>
<p class="dim">Currency and quota only. Whether a mirror holds every object is not
observable from a client and is not claimed here.</p>
<table><tr><th>endpoint</th><th>state</th><th>data lag</th><th>slots</th><th>error</th></tr>
{mrows}</table>
</body>"""
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return html_path, json_path
