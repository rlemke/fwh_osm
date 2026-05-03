"""Clip a cached OSM PBF to a bbox or polygon via ``osmium extract``.

Thin CLI wrapper around ``_lib.pbf_clip.clip_pbf``. The clipped PBF
lands in the regular ``pbf/`` cache under ``clips/<name>-latest.osm.pbf``
so every downstream tool (``convert-pbf-geojson``,
``convert-pbf-shapefile``, ``extract``, ``build-graphhopper-graph``,
``build-valhalla-tiles``) treats the clip as a normal region called
``clips/<name>`` — no special casing required.

Usage::

    # Clip Liechtenstein to its southwest corner
    python clip_pbf.py --source europe/liechtenstein \\
        --bbox 9.47,47.05,9.55,47.10 \\
        --name vaduz-area

    # Clip using a polygon from a GeoJSON file
    python clip_pbf.py --source europe/germany \\
        --polygon bavaria-custom.geojson \\
        --name bavaria-south

    # Rebuild only clips whose source PBF has changed
    python clip_pbf.py --update-all

    # Inspect recorded clips
    python clip_pbf.py --list

Requires ``osmium-tool`` (install via ``tools/install-tools.sh``).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import sidecar  # noqa: E402
from _lib.pbf_clip import (  # noqa: E402
    CACHE_TYPE,
    NAMESPACE,
    SOURCE_CACHE_TYPE,
    DEFAULT_TIMEOUT_SECONDS,
    ClipError,
    ClipResult,
    ClipSpec,
    build_spec,
    clip_abs_path,
    clip_pbf,
    clip_rel_path,
    list_clips,
)


def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"--bbox must be 4 comma-separated floats (west,south,east,north); got {raw!r}"
        )
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--bbox values must be floats: {exc}") from exc
    return (w, s, e, n)


def _clip_info(entry: dict) -> dict:
    return (entry.get("extra") or {}).get("clip") or {}


def _name_from_entry(entry: dict) -> str:
    rel = entry.get("relative_path", "")
    suffix = "-latest.osm.pbf"
    if rel.endswith(suffix):
        return rel[: -len(suffix)]
    return ""


def _up_to_date_cheap(entry: dict) -> bool:
    """Cheap local-only check for one clip sidecar."""
    clip = _clip_info(entry)
    source_region = clip.get("source_region") or ""
    if not source_region:
        return False
    source_rel = f"{source_region}-latest.osm.pbf"
    source_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, source_rel)
    if not source_side:
        return False
    if source_side.get("sha256") != clip.get("source_sha256"):
        return False
    name = _name_from_entry(entry)
    if not name:
        return False
    out_abs = clip_abs_path(name)
    if not out_abs.exists():
        return False
    return out_abs.stat().st_size == entry.get("size_bytes")


def _spec_from_entry(entry: dict) -> ClipSpec:
    return ClipSpec.from_dict(_clip_info(entry))


def _rebuild_existing(name: str, *, force: bool, dry_run: bool, osmium_bin: str) -> str:
    """Rebuild a clip given only its name — pulls source and spec from the sidecar."""
    rel = clip_rel_path(name)
    entry = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel)
    if not entry or "clip" not in (entry.get("extra") or {}):
        raise ClipError(f"no clip sidecar for {name!r}")
    clip_info = _clip_info(entry)
    source_region = clip_info.get("source_region", "")
    if not source_region:
        raise ClipError(f"clip {name!r} sidecar missing source_region")
    spec = _spec_from_entry(entry)

    if dry_run:
        print(
            f"[{name}] would re-clip from {source_region} "
            f"({spec.kind}) -> {clip_abs_path(name)}",
            file=sys.stderr,
        )
        return "dry-run"

    if spec.kind == "bbox":
        result = clip_pbf(
            name,
            source_region,
            bbox=spec.bbox,
            force=force,
            osmium_bin=osmium_bin,
        )
    elif spec.kind == "polygon":
        if not spec.polygon_path or not Path(spec.polygon_path).is_file():
            raise ClipError(
                f"polygon file for {name!r} no longer exists at {spec.polygon_path!r}"
            )
        result = clip_pbf(
            name,
            source_region,
            polygon_path=spec.polygon_path,
            force=force,
            osmium_bin=osmium_bin,
        )
    else:
        raise ClipError(f"unknown clip kind {spec.kind!r} for {name!r}")

    if result.was_cached:
        print(f"[{name}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{name}] done ({result.size_bytes / (1024 * 1024):.1f} MiB, "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "clipped"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clip a cached OSM PBF to a bbox or polygon via osmium extract.",
    )
    parser.add_argument(
        "--source",
        help="Source region in the pbf cache (e.g. europe/germany). "
        "Required when creating a new clip.",
    )
    parser.add_argument(
        "--name",
        help="Clip name — becomes clips/<name> in the pbf manifest. "
        "Must not contain '/'.",
    )
    parser.add_argument(
        "--bbox",
        type=_parse_bbox,
        metavar="W,S,E,N",
        help="Bounding box: west,south,east,north (degrees, EPSG:4326).",
    )
    parser.add_argument(
        "--polygon",
        metavar="PATH",
        help="Path to a polygon file osmium understands (GeoJSON or osmium poly).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every clip recorded in the pbf manifest and exit.",
    )
    parser.add_argument(
        "--update-all",
        action="store_true",
        help="Re-clip every existing clip whose source PBF has changed. "
        "Skip clips whose source SHA still matches.",
    )
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="List existing clips that are stale (source SHA mismatch or "
        "file missing) and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-clip even when the manifest reports an up-to-date cached copy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without running osmium.",
    )
    parser.add_argument(
        "--osmium",
        default="osmium",
        help="Path to the osmium binary (default: 'osmium' on PATH).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Per-clip wall-clock limit (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    args = parser.parse_args()

    # --list / --list-missing / --update-all don't need a source/name pair.
    if args.list:
        clips = list_clips()
        for entry in clips:
            clip = _clip_info(entry)
            desc = ""
            if clip.get("kind") == "bbox":
                desc = f"bbox={clip.get('bbox')}"
            elif clip.get("kind") == "polygon":
                desc = f"polygon={clip.get('polygon_path')}"
            print(
                f"{entry.get('relative_path', '')}\t"
                f"source={clip.get('source_region', '')}\t{desc}"
            )
        print(f"{len(clips)} clip(s)", file=sys.stderr)
        return 0

    if args.list_missing:
        clips = list_clips()
        stale = [e for e in clips if not _up_to_date_cheap(e)]
        for entry in stale:
            print(entry.get("relative_path", ""))
        print(
            f"{len(stale)} stale of {len(clips)} clip(s)",
            file=sys.stderr,
        )
        return 0

    if not args.dry_run and shutil.which(args.osmium) is None and not Path(args.osmium).is_file():
        print(
            f"error: osmium binary not found ({args.osmium!r}). "
            "Install via tools/install-tools.sh.",
            file=sys.stderr,
        )
        return 2

    if args.update_all:
        clips = list_clips()
        stale = [e for e in clips if not _up_to_date_cheap(e)]
        current = len(clips) - len(stale)
        print(
            f"update-all: {len(clips)} clip(s), {len(stale)} need rebuild "
            f"({current} already current)",
            file=sys.stderr,
        )
        if not stale:
            print("update-all: nothing to do, all clips are current", file=sys.stderr)
            return 0
        results = {"clipped": 0, "skipped": 0, "dry-run": 0, "failed": 0}
        failures: list[tuple[str, str]] = []
        for entry in stale:
            name = _name_from_entry(entry)
            try:
                outcome = _rebuild_existing(
                    name, force=args.force, dry_run=args.dry_run, osmium_bin=args.osmium
                )
                results[outcome] += 1
            except ClipError as exc:
                results["failed"] += 1
                failures.append((name, str(exc)))
                print(f"[{name}] FAILED: {exc}", file=sys.stderr)
        print(
            f"\nSummary: {results['clipped']} clipped, {results['skipped']} skipped, "
            f"{results['dry-run']} dry-run, {results['failed']} failed",
            file=sys.stderr,
        )
        return 1 if failures else 0

    # Creating or forcing a single clip.
    if not args.name:
        parser.error("--name is required when creating a clip")
    if not args.source:
        parser.error("--source is required when creating a clip")
    if args.bbox is None and args.polygon is None:
        parser.error("supply one of --bbox or --polygon")

    if args.dry_run:
        print(
            f"[{args.name}] would clip {args.source} -> {clip_abs_path(args.name)} "
            f"({'bbox' if args.bbox else 'polygon'})",
            file=sys.stderr,
        )
        return 0

    try:
        result: ClipResult = clip_pbf(
            args.name,
            args.source,
            bbox=args.bbox,
            polygon_path=args.polygon,
            force=args.force,
            osmium_bin=args.osmium,
            timeout_seconds=args.timeout,
        )
    except ClipError as exc:
        print(f"[{args.name}] FAILED: {exc}", file=sys.stderr)
        return 1

    if result.was_cached:
        print(
            f"[{args.name}] up-to-date ({result.size_bytes / (1024 * 1024):.1f} MiB)",
            file=sys.stderr,
        )
    else:
        print(
            f"[{args.name}] done ({result.size_bytes / (1024 * 1024):.1f} MiB, "
            f"{result.duration_seconds:.1f}s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
