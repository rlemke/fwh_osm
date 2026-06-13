"""GraphHopper routing-graph build library.

Directory artifacts (nodes / edges / properties / geometry / ...) live
at ``cache/osm/graphhopper/<region>-latest/<profile>/`` with a sibling
sidecar at ``<region>-latest/<profile>.meta.json``.

Cache validity requires:
- The source PBF's SHA-256 still matches what the sidecar recorded, AND
- The recorded ``graphhopper_version`` matches the current constant.

Local-backend only — the graph is a directory tree HDFS does not
currently support. Requires Java 17+ and a GraphHopper 8.x ``-web.jar``.
Jar resolution: ``--jar`` → ``$GRAPHHOPPER_JAR`` → ``~/.graphhopper/graphhopper-web.jar``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _osm_tools import sidecar
from _osm_tools.storage import LocalStorage, Storage, get_storage

NAMESPACE = "osm"
SOURCE_CACHE_TYPE = "pbf"
OUTPUT_CACHE_TYPE = "graphhopper"

GRAPHHOPPER_VERSION = "8.0"

PROFILES: tuple[str, ...] = (
    "car",
    "bike",
    "foot",
    "motorcycle",
    "truck",
    "hike",
    "mtb",
    "racingbike",
)

_MOTORIZED_PROFILES = {"car", "motorcycle", "truck"}
_NON_MOTORIZED_PROFILES = {"bike", "mtb", "racingbike"}

DEFAULT_JVM_MEMORY = os.environ.get("GRAPHHOPPER_XMX", "4g")
DEFAULT_TIMEOUT_SECONDS = 3600

_build_locks: dict[tuple[str, str], threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock(region: str, profile: str) -> threading.Lock:
    key = (region, profile)
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


@dataclass
class BuildResult:
    region: str
    profile: str
    graph_dir: str
    relative_path: str
    total_size_bytes: int
    node_count: int
    edge_count: int
    graphhopper_version: str
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_url: str
    source_pbf_path: str
    sidecar: dict[str, Any] = field(default_factory=dict)


class BuildError(RuntimeError):
    """Raised when graph build fails."""


def default_jar_path() -> str:
    return os.environ.get(
        "GRAPHHOPPER_JAR", os.path.expanduser("~/.graphhopper/graphhopper-web.jar")
    )


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or get_storage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def graph_rel_path(region: str, profile: str) -> str:
    """Sidecar key — ``<region>-latest/<profile>``."""
    return f"{region}-latest/{profile}"


def graph_abs_path(region: str, profile: str, storage: Any = None) -> Path:
    s = storage or get_storage()
    return Path(sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, graph_rel_path(region, profile), s))


def _staging_dir(region: str, profile: str, storage: Any = None) -> Path:
    s = storage or get_storage()
    # Java writes the graph into a real local directory, so staging must be
    # local. On a remote backend (S3/MinIO, HDFS) graph_abs_path is an
    # ``s3://``/``hdfs://`` URI — unusable as a build target — so always stage
    # under local scratch there (also honoured via AFL_CONVERT_STAGING=tmp).
    if s.name != "local" or (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = os.environ.get("AFL_LOCAL_SCRATCH") or tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-graphhopper-staging" / safe / profile
    out = graph_abs_path(region, profile, storage)
    return out.with_name(out.name + ".tmp")


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _graph_exists(graph_dir: Path) -> bool:
    if not graph_dir.is_dir():
        return False
    for entry in graph_dir.iterdir():
        name = entry.name
        if name.startswith("nodes") or name.startswith("edges"):
            return True
    return False


@dataclass
class GraphStats:
    valid: bool
    node_count: int
    edge_count: int


def read_graph_stats(graph_dir: Path) -> GraphStats:
    """Read GraphHopper's ``properties`` file for node / edge counts."""
    if not _graph_exists(graph_dir):
        return GraphStats(valid=False, node_count=0, edge_count=0)
    properties = graph_dir / "properties"
    if not properties.exists():
        return GraphStats(valid=True, node_count=0, edge_count=0)
    node_count = 0
    edge_count = 0
    try:
        for line in properties.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.strip().partition("=")
            if key == "graph.nodes.count":
                node_count = int(value)
            elif key == "graph.edges.count":
                edge_count = int(value)
    except (OSError, ValueError):
        return GraphStats(valid=True, node_count=0, edge_count=0)
    return GraphStats(valid=node_count > 0, node_count=node_count, edge_count=edge_count)


def is_up_to_date(
    region: str,
    profile: str,
    pbf_side: dict,
    graph_dir: Path,
    storage: Any = None,
) -> bool:
    """True if cached graph matches source PBF SHA and GraphHopper version."""
    if profile not in PROFILES:
        return False
    s = storage or get_storage()
    rel = graph_rel_path(region, profile)
    existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s)
    if not existing:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    extra = existing.get("extra") or {}
    if extra.get("graphhopper_version") != GRAPHHOPPER_VERSION:
        return False
    # On a remote backend graph_dir is an s3:///hdfs:// URI; the sidecar above
    # is written only after a complete finalize, so its presence (already
    # checked) is the cache-validity marker. Only the local backend can stat
    # the tree.
    if s.name == "local" and not _graph_exists(graph_dir):
        return False
    return True


def _build_config_yaml(osm_path: Path, graph_dir: Path, profile: str) -> str:
    if profile in _MOTORIZED_PROFILES:
        ignored = "footway,cycleway,path,pedestrian,steps"
    elif profile in _NON_MOTORIZED_PROFILES:
        ignored = "motorway,trunk"
    else:
        ignored = ""
    lines = [
        "graphhopper:",
        f"  datareader.file: {osm_path}",
        f"  graph.location: {graph_dir}",
        f"  import.osm.ignored_highways: {ignored}",
        "  profiles:",
        f"    - name: {profile}",
        f"      vehicle: {profile}",
        "      custom_model_files: []",
    ]
    return "\n".join(lines) + "\n"


def _run_import(
    osm_path: Path,
    graph_dir: Path,
    profile: str,
    *,
    jar_path: str,
    jvm_memory: str,
    timeout_seconds: int,
) -> str:
    if not Path(jar_path).is_file():
        raise BuildError(
            f"GraphHopper jar not found: {jar_path!r}. "
            "Set $GRAPHHOPPER_JAR or pass --jar."
        )
    graph_dir.mkdir(parents=True, exist_ok=True)

    config_yaml = _build_config_yaml(osm_path, graph_dir, profile)
    config_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", prefix="gh-config-", delete=False
        ) as tmp:
            tmp.write(config_yaml)
            config_path = tmp.name

        cmd = [
            "java",
            f"-Xmx{jvm_memory}",
            "-jar",
            jar_path,
            "import",
            config_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BuildError(
                f"GraphHopper import timed out after {timeout_seconds}s "
                f"(region={osm_path.name}, profile={profile})"
            ) from exc
        except FileNotFoundError as exc:
            raise BuildError(
                "java not found on PATH. Install a JDK 17+ runtime."
            ) from exc
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").splitlines()[-20:]
            raise BuildError(
                "GraphHopper import failed (exit "
                f"{result.returncode}): {'; '.join(stderr_tail) or '(no stderr)'}"
            )
    finally:
        if config_path:
            try:
                os.unlink(config_path)
            except OSError:
                pass
    return result.stdout or ""


# The authoritative summary line, e.g.
#   com.graphhopper.GraphHopper: nodes: 134,737, edges: 163,794
_GRAPH_SUMMARY_RE = re.compile(r"GraphHopper: nodes:\s+([\d,]+),\s+edges:\s+([\d,]+)")
# Fallback: any human-formatted "nodes: N, edges: M" (a SPACE after the colon
# and comma grouping). This deliberately excludes the binary-flush metadata
# "nodes:9,edges:21" (no space after the colon) that would otherwise win.
_GRAPH_COUNTS_RE = re.compile(r"nodes:\s+([\d,]+),\s+edges:\s+([\d,]+)")


def _counts_from_log(import_log: str) -> tuple[int, int]:
    """Extract routing-graph node/edge counts from GraphHopper's import stdout.
    GraphHopper 8.x stores these only in a binary ``properties`` file, so the
    log is the reliable source. Prefer the ``GraphHopper: nodes: …`` summary
    line; otherwise take the largest space-delimited ``nodes: …, edges: …``
    pair. Returns (0, 0) if none is present."""
    text = import_log or ""
    m = _GRAPH_SUMMARY_RE.search(text)
    if m:
        return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")))
    best = (0, 0)
    for mm in _GRAPH_COUNTS_RE.finditer(text):
        n = int(mm.group(1).replace(",", ""))
        e = int(mm.group(2).replace(",", ""))
        if n > best[0]:
            best = (n, e)
    return best


def build_graph(
    region: str,
    profile: str,
    *,
    force: bool = False,
    jar_path: str | None = None,
    jvm_memory: str = DEFAULT_JVM_MEMORY,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    storage: Any = None,
) -> BuildResult:
    """Build or fetch a cached GraphHopper routing graph."""
    if profile not in PROFILES:
        raise BuildError(
            f"unknown profile: {profile!r}. Valid: {', '.join(PROFILES)}"
        )
    s = storage or get_storage()

    pbf_rel = pbf_rel_path(region)
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not pbf_side:
        raise BuildError(
            f"no pbf sidecar for {region!r}; run download-pbf first"
        )
    # Storage ops take the raw URI string — pbf_abs_path()/graph_abs_path()
    # wrap it in Path, which is right for local fs but collapses ``s3://`` to
    # ``s3:/``. Use the string form for the backend, Path only for local reads.
    pbf_uri = sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not s.exists(pbf_uri):
        raise BuildError(
            f"pbf missing in cache for {region!r}: {pbf_uri}; run download first"
        )
    # Java reads a local file — pull the PBF down from S3/MinIO (no-op on local).
    src_pbf = Path(s.localize(pbf_uri))
    if not src_pbf.exists():
        raise BuildError(f"pbf file missing after localize: {src_pbf}")
    source_url = pbf_side.get("source", {}).get("url", "")

    jar = jar_path or default_jar_path()

    with _build_lock(region, profile):
        rel = graph_rel_path(region, profile)
        graph_uri = sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s)
        graph_dir = graph_abs_path(region, profile, s)  # Path — local fs reads only

        if not force and is_up_to_date(region, profile, pbf_side, graph_dir, s):
            existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s) or {}
            # Counts come from the sidecar (recorded at build time from the
            # import log) on both backends — GraphHopper's on-disk ``properties``
            # is binary and can't be re-parsed for counts.
            extra = existing.get("extra") or {}
            node_count = int(extra.get("node_count") or 0)
            edge_count = int(extra.get("edge_count") or 0)
            size_bytes = existing.get(
                "size_bytes", _dir_size(graph_dir) if s.name == "local" else 0
            )
            return BuildResult(
                region=region,
                profile=profile,
                graph_dir=graph_uri,
                relative_path=rel + "/",
                total_size_bytes=size_bytes,
                node_count=node_count,
                edge_count=edge_count,
                graphhopper_version=GRAPHHOPPER_VERSION,
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_url=source_url,
                source_pbf_path=str(src_pbf),
                sidecar=existing,
            )

        staging = _staging_dir(region, profile, s)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        try:
            import_log = _run_import(
                src_pbf,
                staging,
                profile,
                jar_path=jar,
                jvm_memory=jvm_memory,
                timeout_seconds=timeout_seconds,
            )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        elapsed = time.monotonic() - start

        # GraphHopper 8.x writes a BINARY ``properties`` file, so node/edge
        # counts cannot be grepped from it — the old read_graph_stats path
        # returns 0 and falsely rejects a perfectly good graph. Read counts from
        # the import log and validate by the presence of a non-empty ``nodes``
        # payload instead.
        node_count, edge_count = _counts_from_log(import_log)
        nodes_file = staging / "nodes"
        if not nodes_file.exists() or nodes_file.stat().st_size == 0:
            shutil.rmtree(staging, ignore_errors=True)
            raise BuildError(
                f"GraphHopper produced no graph for {region}/{profile}"
            )

        # Capture size + payload hash from the LOCAL staging tree *before*
        # finalize: finalize uploads then deletes staging, and on a remote
        # backend graph_dir is an s3:///hdfs:// URI local fs ops can't read.
        total_size = _dir_size(staging)
        primary_sha = _sha256_file(staging / "nodes") if (staging / "nodes").exists() else ""

        s.finalize_dir_from_local(str(staging), graph_uri)

        generated_at = sidecar.utcnow_iso()

        side = sidecar.write_sidecar(
            NAMESPACE,
            OUTPUT_CACHE_TYPE,
            rel,
            kind="directory",
            size_bytes=total_size,
            sha256=primary_sha,
            source={
                "namespace": NAMESPACE,
                "cache_type": SOURCE_CACHE_TYPE,
                "relative_path": pbf_rel,
                "sha256": pbf_side.get("sha256"),
                "size_bytes": pbf_side.get("size_bytes"),
                "source_checksum": pbf_side.get("source", {}).get("source_checksum"),
                "source_timestamp": pbf_side.get("source", {}).get("source_timestamp"),
                "downloaded_at": pbf_side.get("source", {}).get("downloaded_at"),
            },
            tool={
                "command": "java -jar graphhopper-web.jar import",
                "jar_path": jar,
                "jvm_memory": jvm_memory,
            },
            extra={
                "region": region,
                "profile": profile,
                "graphhopper_version": GRAPHHOPPER_VERSION,
                "node_count": node_count,
                "edge_count": edge_count,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return BuildResult(
            region=region,
            profile=profile,
            graph_dir=graph_uri,
            relative_path=rel + "/",
            total_size_bytes=total_size,
            node_count=node_count,
            edge_count=edge_count,
            graphhopper_version=GRAPHHOPPER_VERSION,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            source_url=source_url,
            source_pbf_path=str(src_pbf),
            sidecar=side,
        )


def _sha256_file(path: Path) -> str:
    import hashlib
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def clean_graph(graph_dir: str) -> bool:
    """Remove a built graph directory. Returns True if something was deleted."""
    p = Path(graph_dir)
    if p.exists():
        shutil.rmtree(p)
        return True
    return False


def to_graphhopper_cache(result: BuildResult) -> dict[str, Any]:
    """Map a ``BuildResult`` to the ``GraphHopperCache`` FFL schema dict."""
    return {
        "osmSource": result.source_pbf_path,
        "graphDir": result.graph_dir,
        "profile": result.profile,
        "date": result.generated_at,
        "size": result.total_size_bytes,
        "wasInCache": result.was_cached,
        "version": result.graphhopper_version,
        "nodeCount": result.node_count,
        "edgeCount": result.edge_count,
    }
