"""Convert cached OSM PBF files to multi-layer ESRI Shapefile bundles.

Thin CLI wrapper around ``_lib.pbf_shapefile.convert_region``. Both
this tool and the FFL ``osm.ops.ConvertPbfToShapefile`` handler call
that library, so they share one cache layout and one manifest.

Output for each region is a **directory** of bundles (one
``.shp``/``.shx``/``.dbf``/``.prj``/``.cpg`` set per layer):

    <cache_root>/shapefiles/<region>-latest/
        points.shp .shx .dbf .prj .cpg
        lines.shp ...
        multilinestrings.shp ...
        multipolygons.shp ...

The manifest records the source PBF's SHA-256 and the requested layer
set, so reruns skip regions whose source PBF hasn't changed and whose
cached layers cover (⊇) the current request.

Requires the ``ogr2ogr`` command-line tool (GDAL) on ``PATH``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import sidecar  # noqa: E402
from _lib.pbf_download import (  # noqa: E402
    filter_leaves,
    regions_from_pbf_cache,
)
from _lib.pbf_shapefile import (  # noqa: E402
    NAMESPACE,
    OSM_LAYER_NAMES,
    SOURCE_CACHE_TYPE,
    ConversionError,
    ConvertResult,
    convert_region,
    is_up_to_date,
    normalize_layers,
    pbf_abs_path,
    shapefile_abs_path,
)

DEFAULT_JOBS = 2

# Per-layer human descriptions for --help epilog only.
OSM_LAYER_DESCRIPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "points",
        "Point",
        "Tagged nodes — POIs, amenities, places, shops, addresses, etc.",
    ),
    (
        "lines",
        "LineString",
        "Non-area linear ways — highways, railways, waterways, barriers, "
        "power lines.",
    ),
    (
        "multilinestrings",
        "MultiLineString",
        "OSM relations forming linear structures — route relations (bus, "
        "bicycle, hiking, ski).",
    ),
    (
        "multipolygons",
        "MultiPolygon",
        "Closed ways + type=multipolygon / type=boundary relations — "
        "buildings, land use, natural areas, admin boundaries.",
    ),
)


def _help_epilog() -> str:
    layer_lines = []
    for name, geom, desc in OSM_LAYER_DESCRIPTIONS:
        layer_lines.append(f"  {name:<17} ({geom}) — {desc}")
    return (
        "layers produced per region (GDAL OSM driver convention):\n"
        + "\n".join(layer_lines)
        + "\n\n"
        "  other_relations     (skipped) — GeometryCollection; shapefile "
        "can't represent it.\n"
        "                       Use convert-pbf-geojson if you need these.\n"
        "\n"
        "related layer-naming conventions in the OSM ecosystem (reference only):\n"
        "  osm2pgsql (PostGIS)  planet_osm_point / _line / _polygon / _roads\n"
        "  Geofabrik downloads  gis_osm_<category>_free_1 "
        "(roads, buildings, waterways, ...)\n"
        "\n"
        "output format:\n"
        "  ESRI Shapefile (.shp/.shx/.dbf/.prj/.cpg per layer). Format limits:\n"
        "  - field names truncated to 10 chars (ogr2ogr warns)\n"
        "  - each .shp capped at 2 GiB — very large regions may hit this\n"
        "  - one geometry type per .shp — hence the multi-layer output"
    )


def _read_regions_file(path: Path) -> list[str]:
    regions: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        regions.append(line)
    return regions


def _up_to_date_cheap(region: str, layers: tuple[str, ...]) -> bool:
    pbf_rel = f"{region}-latest.osm.pbf"
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel)
    if not pbf_side:
        return False
    return is_up_to_date(region, pbf_side, shapefile_abs_path(region), layers)


def _run_one(
    region: str,
    *,
    layers: tuple[str, ...],
    force: bool,
    dry_run: bool,
    ogr2ogr_bin: str,
) -> str:
    if dry_run:
        src = pbf_abs_path(region)
        dst = shapefile_abs_path(region)
        print(
            f"[{region}] would convert {src} -> {dst}/ "
            f"(layers: {','.join(layers)})",
            file=sys.stderr,
        )
        return "dry-run"
    try:
        result: ConvertResult = convert_region(
            region, layers=layers, force=force, ogr2ogr_bin=ogr2ogr_bin
        )
    except ConversionError as exc:
        print(f"[{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{region}] done ({result.total_size_bytes / (1024 * 1024):.1f} MiB "
        f"total, {len(result.layers)} layer(s): "
        f"{','.join(l['name'] for l in result.layers)}, "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "converted"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert cached OSM PBF files to ESRI Shapefile bundles via ogr2ogr.",
        epilog=_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("regions", nargs="*")
    parser.add_argument("--regions-file", type=Path)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--all-under", metavar="PREFIX")
    parser.add_argument("--include-parents", action="store_true")
    parser.add_argument("--update-all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--layers",
        default=",".join(OSM_LAYER_NAMES),
        metavar="LIST",
        help=f"Comma-separated layers to emit. Valid: {', '.join(OSM_LAYER_NAMES)}. "
        f"Default: all four.",
    )
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument("--ogr2ogr", default="ogr2ogr")
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    ogr2ogr_bin = args.ogr2ogr
    if shutil.which(ogr2ogr_bin) is None and not Path(ogr2ogr_bin).is_file():
        print(
            f"error: ogr2ogr binary not found ({ogr2ogr_bin!r}). "
            "Install GDAL (e.g. 'brew install gdal' or 'apt install gdal-bin').",
            file=sys.stderr,
        )
        return 2

    try:
        layers_tuple = normalize_layers(args.layers)
    except ConversionError as exc:
        parser.error(str(exc))

    regions: list[str] = list(args.regions)
    if args.regions_file:
        regions.extend(_read_regions_file(args.regions_file))

    if args.all or args.all_under is not None:
        from_manifest = regions_from_pbf_cache(under=args.all_under)
        before = len(from_manifest)
        if not args.include_parents:
            from_manifest = filter_leaves(from_manifest)
        print(
            f"pbf manifest: {before} region(s) matched, "
            f"{len(from_manifest)} selected after "
            f"{'leaves-only' if not args.include_parents else 'include-parents'} filter",
            file=sys.stderr,
        )
        regions.extend(from_manifest)

    if args.update_all:
        from_manifest = regions_from_pbf_cache()
        if not args.include_parents:
            from_manifest = filter_leaves(from_manifest)
        needs_work = [
            r for r in from_manifest if not _up_to_date_cheap(r, layers_tuple)
        ]
        print(
            f"update-all: {len(from_manifest)} cached pbf(s), "
            f"{len(needs_work)} need conversion "
            f"({len(from_manifest) - len(needs_work)} already current)",
            file=sys.stderr,
        )
        regions.extend(needs_work)

    seen: set[str] = set()
    deduped: list[str] = []
    for r in regions:
        if r and r not in seen:
            seen.add(r)
            deduped.append(r)
    regions = deduped

    if not regions:
        if args.update_all and not (args.all or args.all_under or args.regions):
            print(
                "update-all: nothing to do, all shapefile bundles are current",
                file=sys.stderr,
            )
            return 0
        parser.error(
            "no regions provided (pass as args, or use --regions-file, "
            "--all, --all-under, or --update-all)"
        )

    if args.list:
        for r in regions:
            print(r)
        print(f"{len(regions)} region(s)", file=sys.stderr)
        return 0

    results = {"converted": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    def _run(region: str) -> tuple[str, str | None]:
        try:
            outcome = _run_one(
                region,
                layers=layers_tuple,
                force=args.force,
                dry_run=args.dry_run,
                ogr2ogr_bin=ogr2ogr_bin,
            )
            return outcome, None
        except Exception as exc:  # noqa: BLE001
            return "failed", str(exc)

    if args.jobs == 1 or len(regions) == 1:
        for region in regions:
            outcome, err = _run(region)
            if err:
                failures.append((region, err))
            results[outcome] += 1
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_run, r): r for r in regions}
            for fut in concurrent.futures.as_completed(futures):
                region = futures[fut]
                outcome, err = fut.result()
                if err:
                    failures.append((region, err))
                results[outcome] += 1

    print(
        f"\nSummary: {results['converted']} converted, "
        f"{results['skipped']} skipped, "
        f"{results['dry-run']} dry-run, "
        f"{results['failed']} failed",
        file=sys.stderr,
    )
    if failures:
        for region, msg in failures:
            print(f"  - {region}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
