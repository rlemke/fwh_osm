"""Phase 1 bootstrap (Strategy A): planet PBF -> Geofabrik-style regional split.

Shared library behind the ``planet_bootstrap.py`` tool. One read of the source
(``planet-latest.osm.pbf`` in production, a small stand-in in tests) is split by
an ``osmium extract`` multi-region config into per-region PBFs; each output is
stamped with OUR ``osmosis_replication_*`` header so the existing delta path
(``pbf_update._apply_geofabrik_diffs`` -> ``osmium.replication.get_replication_header``
-> ``ReplicationServer``) follows our OWN server instead of Geofabrik. Emits the
Geofabrik-compatible layout::

    <out>/<region>-latest.osm.pbf
    <out>/<region>-updates/state.txt

so serving ``<out>`` over HTTP and pointing ``FW_GEOFABRIK_BASE_URL`` at it needs
no code change. Scaling to real planet == swap ``source`` for
``planet-latest.osm.pbf`` (it carries its own replication header, same as the
regional stand-ins), swap the region spec for real ``.poly`` files
(Geofabrik/OSM admin boundaries), and provision the node-location index disk that
``--strategy smart``/``complete_ways`` needs at planet scale.

Requires the ``osmium`` binary (osmium-tool) and pyosmium (already a dependency of
the update path).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Callable, Iterable

from osmium.replication import get_replication_header

STRATEGIES = ("simple", "complete_ways", "smart")


class BootstrapError(RuntimeError):
    """A bootstrap step failed (bad region spec, osmium error, header mismatch)."""


@dataclass
class RegionResult:
    key: str
    path: str
    nodes: int
    ways: int
    replication_url: str
    sequence: int | None
    header_ok: bool


def bbox_poly(name: str, bbox: Iterable[float]) -> str:
    """``.poly`` text for a bounding box ``(min_lon, min_lat, max_lon, max_lat)``.

    A convenience for prototyping and axis-aligned tiles. Real regions supply
    their own ``.poly`` (an admin boundary), which is what makes the split follow
    country/state edges rather than a rectangle.
    """
    x0, y0, x1, y1 = bbox
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    body = [name, "1"] + [f"   {x:.7E}   {y:.7E}" for x, y in ring] + ["END", "END", ""]
    return "\n".join(body)


def state_txt(seq: int | None, ts_iso: str | None) -> str:
    """OSM replication ``state.txt`` (Java-properties: colons in the timestamp escaped)."""
    out = ""
    if seq is not None:
        out += f"sequenceNumber={seq}\n"
    if ts_iso:
        out += "timestamp=" + ts_iso.replace(":", r"\:") + "\n"
    return out


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:  # osmium binary missing
        raise BootstrapError(f"required binary not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise BootstrapError(f"command failed ({exc.returncode}): {' '.join(cmd)}") from exc


def _feature_counts(pbf: str) -> tuple[int, int]:
    """(nodes, ways) via osmium fileinfo; (0, 0) if the file is missing/empty/unreadable."""
    try:
        info = json.loads(subprocess.check_output(
            ["osmium", "fileinfo", "-ej", pbf], stderr=subprocess.DEVNULL))
        cnt = info["data"]["count"]
        return cnt["nodes"], cnt["ways"]
    except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError, FileNotFoundError):
        return 0, 0


def bootstrap(
    *,
    source: str,
    out: str,
    regions: list[dict],
    base_url: str,
    strategy: str = "smart",
    on_log: Callable[[str], None] | None = None,
) -> list[RegionResult]:
    """Split ``source`` into ``regions`` and stamp each with our replication header.

    ``regions`` is a list of ``{"key": "<region/path>", "bbox": [..]}`` or
    ``{"key": ..., "poly": "<path.poly>"}``. Returns one :class:`RegionResult`
    per region; raises :class:`BootstrapError` on any failure (including a header
    round-trip mismatch, which would mean the delta path can't follow us).
    """
    if strategy not in STRATEGIES:
        raise BootstrapError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")
    log = on_log or (lambda _m: None)

    out_dir = Path(out)
    poly_dir = out_dir / "_poly"
    poly_dir.mkdir(parents=True, exist_ok=True)

    # 1. Source replication position -> the baseline every region inherits (all
    #    regions are as-of the same planet snapshot).
    src = get_replication_header(source)
    seq = src.sequence
    ts_iso = (
        src.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if src.timestamp else None
    )
    log(f"source seq={seq} ts={ts_iso} (from {src.url or 'no replication header'})")

    # 2. osmium multi-extract config (one output per region; ABSOLUTE poly paths,
    #    since osmium resolves a relative polygon file_name against `directory`).
    extracts = []
    for r in regions:
        if "key" not in r or not ("bbox" in r or "poly" in r):
            raise BootstrapError(f"region needs 'key' and one of 'bbox'/'poly': {r!r}")
        safe = r["key"].replace("/", "__")
        if "poly" in r:
            polyfile = str(Path(r["poly"]).resolve())
        else:
            pf = poly_dir / f"{safe}.poly"
            pf.write_text(bbox_poly(r["key"], r["bbox"]))
            polyfile = str(pf.resolve())
        extracts.append({
            "output": f"{safe}.osm.pbf",
            "polygon": {"file_name": polyfile, "file_type": "poly"},
        })
    cfg_path = out_dir / "extract-config.json"
    cfg_path.write_text(json.dumps({"directory": str(out_dir), "extracts": extracts}, indent=2))

    # 3. Single pass over the source -> all regional PBFs.
    log(f"osmium extract ({strategy}) -> {len(extracts)} region(s)")
    _run(["osmium", "extract", "-c", str(cfg_path), source, "--strategy", strategy, "--overwrite"])

    # 4. Stamp OUR replication header on each output, publish the Geofabrik-style
    #    layout, and verify the round-trip through the delta path's reader.
    results: list[RegionResult] = []
    for r in regions:
        key = r["key"]
        safe = key.replace("/", "__")
        raw = out_dir / f"{safe}.osm.pbf"
        # A region with zero features yields NO output file (a tiny country empty
        # in the source) OR an empty one (an antimeridian extract) — either would
        # crash the next osmium step. Skip it up front rather than publishing an
        # empty extract.
        nodes, ways = _feature_counts(str(raw)) if raw.exists() else (0, 0)
        if nodes == 0:
            log(f"  {key}: empty region (no features) — skipped")
            raw.unlink(missing_ok=True)
            continue
        final = out_dir / f"{key}-latest.osm.pbf"
        final.parent.mkdir(parents=True, exist_ok=True)
        # Geofabrik convention: the extract's replication URL is its own
        # `<region>-updates/` dir — the same path where we write state.txt below.
        # This makes ONE base URL serve both extracts (`<base>/<region>-latest.osm.pbf`)
        # and replication (`<base>/<region>-updates/`), so FW_GEOFABRIK_BASE_URL
        # pointed at a single static server resolves download AND delta coherently.
        repl_url = f"{base_url.rstrip('/')}/{key}-updates"

        # Some sources (e.g. the full planet dump) expose only a replication
        # timestamp, no sequence — don't stamp the literal "None".
        hdr = [f"--output-header=osmosis_replication_base_url={repl_url}"]
        if seq is not None:
            hdr.append(f"--output-header=osmosis_replication_sequence_number={seq}")
        if ts_iso:
            hdr.append(f"--output-header=osmosis_replication_timestamp={ts_iso}")
        _run(["osmium", "cat", str(raw), "-o", str(final), "--overwrite", *hdr])
        raw.unlink()

        upd = out_dir / f"{key}-updates"
        upd.mkdir(parents=True, exist_ok=True)
        (upd / "state.txt").write_text(state_txt(seq, ts_iso))

        h = get_replication_header(str(final))
        header_ok = (h.url == repl_url and h.sequence == seq)
        if not header_ok:
            raise BootstrapError(
                f"{key}: header round-trip failed "
                f"(url={h.url!r} seq={h.sequence!r}; expected {repl_url!r} {seq!r}) "
                "— the delta path would NOT follow our server"
            )
        results.append(RegionResult(key, str(final), nodes, ways, repl_url, h.sequence, header_ok))
        log(f"  {key}: {nodes} nodes / {ways} ways -> {repl_url}")

    return results


def bootstrap_batched(*, source: str, out: str, regions: list[dict], base_url: str,
                      strategy: str = "smart", batch_size: int = 0,
                      on_log: Callable[[str], None] | None = None) -> list[RegionResult]:
    """Like :func:`bootstrap` but splits ``regions`` into batches.

    osmium holds a node-id set per region in one extract pass, so extracting
    hundreds of regions at once can exhaust RAM. ``batch_size`` (0 = single pass)
    bounds peak memory by processing that many regions per osmium pass, at the
    cost of re-reading ``source`` once per batch. Returns the concatenated results.
    """
    log = on_log or (lambda _m: None)
    if batch_size <= 0 or batch_size >= len(regions):
        return bootstrap(source=source, out=out, regions=regions, base_url=base_url,
                         strategy=strategy, on_log=on_log)
    results: list[RegionResult] = []
    nbatches = (len(regions) + batch_size - 1) // batch_size
    for i in range(0, len(regions), batch_size):
        batch = regions[i:i + batch_size]
        log(f"batch {i // batch_size + 1}/{nbatches}: {len(batch)} regions")
        results.extend(bootstrap(source=source, out=out, regions=batch, base_url=base_url,
                                 strategy=strategy, on_log=on_log))
    return results
