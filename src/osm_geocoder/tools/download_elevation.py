"""Download cropped elevation rasters (Copernicus DEM GLO-30) for a bbox.

Each call produces one GeoTIFF at ``elevation/<name>-latest.tif``.
Uses ``gdalwarp`` with ``/vsicurl/`` to mosaic upstream S3 tiles and
crop to the requested bbox in one shot.

Usage::

    python download_elevation.py --name liechtenstein --bbox 9.47,47.05,9.65,47.30
    python download_elevation.py --list
    python download_elevation.py --update-all

Requires GDAL (``brew install gdal`` or ``tools/install-tools.sh``).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.elevation_download import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    ELEVATION_VERSION,
    SOURCE_COP_DEM_30M,
    SUPPORTED_SOURCES,
    DownloadResult,
    ElevationError,
    download_elevation,
    is_up_to_date,
    list_rasters,
)


def _parse_bbox(raw: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"--bbox must be 4 comma-separated floats (west,south,east,north)"
        )
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--bbox values must be floats: {exc}") from exc
    return (w, s, e, n)


def _run_one(
    name: str,
    bbox: tuple[float, float, float, float],
    *,
    source: str,
    force: bool,
    dry_run: bool,
    gdalwarp_bin: str,
    timeout_seconds: int,
) -> str:
    if dry_run:
        print(f"[{name}] would fetch elevation for bbox={bbox} source={source}", file=sys.stderr)
        return "dry-run"
    try:
        result: DownloadResult = download_elevation(
            name,
            bbox,
            source=source,
            force=force,
            gdalwarp_bin=gdalwarp_bin,
            timeout_seconds=timeout_seconds,
        )
    except ElevationError as exc:
        print(f"[{name}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{name}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{name}] done "
        f"({result.size_bytes / (1024 * 1024):.1f} MiB, "
        f"{len(result.tile_urls)} source tile(s), "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "downloaded"


def _rebuild_existing(entry: dict, *, force: bool, dry_run: bool, gdalwarp_bin: str, timeout_seconds: int) -> str:
    name = entry.get("name", "")
    bbox_list = entry.get("bbox", [])
    source = entry.get("source", "")
    if not name or len(bbox_list) != 4 or source not in SUPPORTED_SOURCES:
        raise ElevationError(
            f"manifest entry incomplete for {name!r}: source={source}, bbox={bbox_list}"
        )
    bbox = (float(bbox_list[0]), float(bbox_list[1]), float(bbox_list[2]), float(bbox_list[3]))
    return _run_one(
        name,
        bbox,
        source=source,
        force=force,
        dry_run=dry_run,
        gdalwarp_bin=gdalwarp_bin,
        timeout_seconds=timeout_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download Copernicus DEM GLO-30 elevation rasters for a bbox.",
    )
    parser.add_argument("--name", help="Cache entry name (file becomes <name>-latest.tif).")
    parser.add_argument("--bbox", type=_parse_bbox, metavar="W,S,E,N", help="Bounding box in EPSG:4326.")
    parser.add_argument(
        "--source",
        default=SOURCE_COP_DEM_30M,
        choices=list(SUPPORTED_SOURCES),
        help=f"DEM source (default: {SOURCE_COP_DEM_30M}).",
    )
    parser.add_argument("--list", action="store_true", help="List recorded rasters.")
    parser.add_argument(
        "--update-all",
        action="store_true",
        help=f"Rebuild every entry whose elevation_version != {ELEVATION_VERSION} (i.e. after a toolchain upgrade).",
    )
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="List entries that need rebuild (missing file or version mismatch).",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gdalwarp", default="gdalwarp")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    if args.list:
        rasters = list_rasters()
        for e in rasters:
            bbox = e.get("bbox", [])
            print(
                f"{e.get('name', ''):<30}\t"
                f"size={e.get('size_bytes', 0)}\t"
                f"bbox={bbox}\t"
                f"source={e.get('source', '')}\t"
                f"v={e.get('elevation_version', '?')}"
            )
        print(f"{len(rasters)} raster(s)", file=sys.stderr)
        return 0

    if args.list_missing:
        rasters = list_rasters()
        stale: list[dict] = []
        for e in rasters:
            bbox_list = e.get("bbox", [])
            if len(bbox_list) != 4:
                stale.append(e)
                continue
            bbox = (float(bbox_list[0]), float(bbox_list[1]), float(bbox_list[2]), float(bbox_list[3]))
            if not is_up_to_date(e.get("name", ""), e.get("source", ""), bbox):
                stale.append(e)
        for e in stale:
            print(e.get("name", ""))
        print(f"{len(stale)} stale of {len(rasters)} raster(s)", file=sys.stderr)
        return 0

    if not args.dry_run and shutil.which(args.gdalwarp) is None and not Path(args.gdalwarp).is_file():
        print(
            f"error: {args.gdalwarp!r} not found. "
            "Install GDAL via tools/install-tools.sh or 'brew install gdal'.",
            file=sys.stderr,
        )
        return 2

    if args.update_all:
        rasters = list_rasters()
        if not rasters:
            print("update-all: manifest is empty, nothing to refresh", file=sys.stderr)
            return 0
        results = {"downloaded": 0, "skipped": 0, "dry-run": 0, "failed": 0}
        failures: list[tuple[str, str]] = []
        for e in rasters:
            name = e.get("name", "")
            try:
                outcome = _rebuild_existing(
                    e,
                    force=args.force,
                    dry_run=args.dry_run,
                    gdalwarp_bin=args.gdalwarp,
                    timeout_seconds=args.timeout,
                )
                results[outcome] += 1
            except ElevationError as exc:
                failures.append((name, str(exc)))
                results["failed"] += 1
        print(
            f"\nSummary: {results['downloaded']} downloaded, {results['skipped']} skipped, "
            f"{results['dry-run']} dry-run, {results['failed']} failed",
            file=sys.stderr,
        )
        return 1 if failures else 0

    if not args.name:
        parser.error("--name is required for a single-region download")
    if args.bbox is None:
        parser.error("--bbox is required for a single-region download")

    try:
        outcome = _run_one(
            args.name,
            args.bbox,
            source=args.source,
            force=args.force,
            dry_run=args.dry_run,
            gdalwarp_bin=args.gdalwarp,
            timeout_seconds=args.timeout,
        )
    except ElevationError:
        return 1
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
