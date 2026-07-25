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
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Callable, Iterable

from osmium.replication import get_replication_header

STRATEGIES = ("simple", "complete_ways", "smart")

# Adaptive-batching defaults. osmium extract holds a per-region node-id set (plus,
# for complete_ways/smart, a node-location index), so peak RAM scales with the
# regions packed into one pass. Rather than guess a fixed batch_size, we detect the
# host/container memory CEILING, keep each pass under `budget = ceiling * FRACTION`,
# MEASURE the pass's real peak, and learn a per-region cost — self-healing on OOM.
_MEM_FRACTION = float(os.environ.get("FW_OSM_MEM_FRACTION", "0.7"))
_DEFAULT_REGION_BYTES = int(2.0 * (1 << 30))   # cold-start estimate (2 GiB/region)
_MAX_REGIONS_PER_PASS = 64                     # backstop so tiny regions don't over-pack
_COST_SIDECAR = ".region_cost_est.json"        # persists the learned per-region cost


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


class _OOMError(BootstrapError):
    """A pass was killed by the OOM killer (SIGKILL / -9) or raised MemoryError —
    recoverable by re-running the same regions in a smaller pass."""


def _memory_ceiling_bytes() -> int:
    """The real memory ceiling this process runs under — the number to size passes
    against. Prefer the cgroup limit (a container's ACTUAL cap), then the machine's
    total RAM. On a Docker-Desktop runner the container's `memory.max` is often
    "max" (unlimited) but the Linux VM only has ~14 GiB, so `/proc/meminfo` is what
    reveals the true ceiling — exactly the number we kept discovering by hand."""
    for path in ("/sys/fs/cgroup/memory.max",                       # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):    # cgroup v1
        try:
            v = open(path).read().strip()
            if v.isdigit() and int(v) < (1 << 62):                  # not "max"/unset
                return int(v)
        except OSError:
            pass
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import psutil
        return int(psutil.virtual_memory().total)
    except Exception:
        return 8 * (1 << 30)   # conservative fallback


def _run_measured(cmd: list[str]) -> int:
    """Run ``cmd`` and return the peak resident memory (bytes) of the child (incl.
    its children), polled best-effort via psutil. Raises :class:`_OOMError` when the
    child is SIGKILL'd (returncode -9 = the OOM killer), else :class:`BootstrapError`.
    Falls back to an unmeasured run (peak 0) when psutil is unavailable."""
    try:
        import psutil
    except ImportError:
        # No measurement, but still CLASSIFY OOM so the adaptive batcher can self-heal.
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError as exc:
            raise BootstrapError(f"required binary not found: {cmd[0]}") from exc
        except subprocess.CalledProcessError as exc:
            if exc.returncode in (-9, 137):
                raise _OOMError(f"osmium killed (OOM, rc={exc.returncode})") from exc
            raise BootstrapError(f"command failed ({exc.returncode}): {' '.join(cmd)}") from exc
        return 0
    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError as exc:
        raise BootstrapError(f"required binary not found: {cmd[0]}") from exc
    peak = 0
    try:
        p = psutil.Process(proc.pid)
        while proc.poll() is None:
            try:
                rss = p.memory_info().rss
                for c in p.children(recursive=True):
                    try:
                        rss += c.memory_info().rss
                    except psutil.Error:
                        pass
                peak = max(peak, rss)
            except psutil.Error:
                break
            time.sleep(0.3)
    finally:
        rc = proc.wait()
    if rc == -9 or rc == 137:                                  # SIGKILL — OOM killer
        raise _OOMError(f"osmium killed (OOM, rc={rc}): {' '.join(cmd[:3])}…")
    if rc != 0:
        raise BootstrapError(f"command failed ({rc}): {' '.join(cmd)}")
    return peak


def _load_region_cost(state_dir: str | None) -> int:
    """Learned per-region peak-cost estimate (bytes), persisted across runs so the
    NEXT split starts calibrated instead of cold. Cold-start default otherwise."""
    if state_dir:
        try:
            v = json.loads(open(os.path.join(state_dir, _COST_SIDECAR)).read())
            est = int(v.get("region_cost_bytes", 0))
            if est > 0:
                return est
        except (OSError, ValueError, KeyError):
            pass
    return _DEFAULT_REGION_BYTES


def _save_region_cost(state_dir: str | None, region_cost_bytes: int) -> None:
    if not state_dir:
        return
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, _COST_SIDECAR), "w") as f:
            json.dump({"region_cost_bytes": int(region_cost_bytes)}, f)
    except OSError:
        pass


def _feature_counts(pbf: str) -> tuple[int, int]:
    """(nodes, ways) via osmium fileinfo; (0, 0) if the file is missing/empty/unreadable."""
    try:
        info = json.loads(subprocess.check_output(
            ["osmium", "fileinfo", "-ej", pbf], stderr=subprocess.DEVNULL))
        cnt = info["data"]["count"]
        return cnt["nodes"], cnt["ways"]
    except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError, FileNotFoundError):
        return 0, 0


def _poly_file_type(path: str) -> str:
    """osmium polygon file type from extension: GeoJSON (TIGER states) or .poly (osmfr)."""
    return "geojson" if path.lower().endswith((".geojson", ".json")) else "poly"


def bootstrap(
    *,
    source: str,
    out: str,
    regions: list[dict],
    base_url: str,
    strategy: str = "complete_ways",
    on_log: Callable[[str], None] | None = None,
    pass_stats: dict | None = None,
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
            "polygon": {"file_name": polyfile, "file_type": _poly_file_type(polyfile)},
        })
    cfg_path = out_dir / "extract-config.json"
    cfg_path.write_text(json.dumps({"directory": str(out_dir), "extracts": extracts}, indent=2))

    # 3. Single pass over the source -> all regional PBFs. When pass_stats is given,
    #    measure the child's peak RSS so the adaptive batcher can learn the cost.
    log(f"osmium extract ({strategy}) -> {len(extracts)} region(s)")
    extract_cmd = ["osmium", "extract", "-c", str(cfg_path), source,
                   "--strategy", strategy, "--overwrite"]
    if pass_stats is not None:
        pass_stats["peak_bytes"] = _run_measured(extract_cmd)
    else:
        _run(extract_cmd)

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
                      strategy: str = "complete_ways", batch_size: int = 0,
                      cost_state_dir: str | None = None,
                      on_log: Callable[[str], None] | None = None) -> list[RegionResult]:
    """Split ``regions`` into memory-bounded osmium passes.

    osmium holds a node-id set per region in one extract pass, so extracting many
    regions at once can exhaust RAM. Two modes:

    - ``batch_size > 0`` — fixed count per pass (manual override / legacy).
    - ``batch_size <= 0`` — **adaptive** (default): detect the memory ceiling, keep
      each pass under a fraction of it, MEASURE the real peak, learn a per-region
      cost, and self-heal on OOM by re-running the offending regions in a smaller
      pass. See :func:`_bootstrap_adaptive`.

    Each pass re-reads ``source``. Returns the concatenated results.
    """
    log = on_log or (lambda _m: None)
    if batch_size and batch_size > 0:
        if batch_size >= len(regions):
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
    return _bootstrap_adaptive(source=source, out=out, regions=regions, base_url=base_url,
                               strategy=strategy, cost_state_dir=cost_state_dir, log=log)


def _bootstrap_adaptive(*, source, out, regions, base_url, strategy, cost_state_dir, log):
    """Memory-budgeted, self-healing region split. Sizes each pass from a learned
    per-region cost against the detected ceiling, measures the actual peak, updates
    the estimate (EWMA, persisted), and on OOM re-runs the same regions smaller."""
    ceiling = _memory_ceiling_bytes()
    budget = max(int(ceiling * _MEM_FRACTION), 1)
    est = _load_region_cost(cost_state_dir)   # bytes/region (persisted or cold default)
    log(f"adaptive: ceiling {ceiling / 1e9:.1f}GB, budget {budget / 1e9:.1f}GB "
        f"(x{_MEM_FRACTION}), start est {est / 1e9:.2f}GB/region")

    results: list[RegionResult] = []
    remaining = list(regions)
    total = len(remaining)
    done = 0
    while remaining:
        n = max(1, min(len(remaining), _MAX_REGIONS_PER_PASS, int(budget // max(est, 1))))
        batch = remaining[:n]
        log(f"adaptive pass: {n} region(s) [{done}/{total} done] "
            f"(est {est * n / 1e9:.1f}GB vs budget {budget / 1e9:.1f}GB)")
        stats: dict = {}
        try:
            res = bootstrap(source=source, out=out, regions=batch, base_url=base_url,
                            strategy=strategy, on_log=log, pass_stats=stats)
        except _OOMError as exc:
            if n <= 1:
                raise BootstrapError(
                    f"single region exceeds the memory budget ({budget / 1e9:.1f}GB): "
                    f"{batch[0].get('key')!r}") from exc
            # Raise the estimate so the retry packs fewer, then re-run the SAME
            # remaining regions (nothing consumed) in a smaller pass.
            est = max(int(est * 1.8), budget // (n - 1) + 1)
            log(f"adaptive: OOM at {n} region(s) → raise est to {est / 1e9:.2f}GB/region, retry smaller")
            _save_region_cost(cost_state_dir, est)
            continue
        results.extend(res)
        remaining = remaining[n:]
        done += n
        peak = stats.get("peak_bytes") or 0
        if peak > 0:
            per = peak / n
            est = int(0.5 * est + 0.5 * per)          # EWMA toward the measured cost
            log(f"adaptive: pass peak {peak / 1e9:.1f}GB ({per / 1e9:.2f}GB/region) "
                f"→ est {est / 1e9:.2f}GB/region")
            _save_region_cost(cost_state_dir, est)
    return results
