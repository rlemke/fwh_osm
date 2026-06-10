"""Filter a GeoJSON FeatureCollection — by a simple tag predicate or by an
arbitrary **Python script**.

This is the shared-library implementation: the FFL handlers (``osm.Filter.*``)
and the ``filter-geojson`` CLI both call into it, so there is exactly one
filtering implementation. It is object-store-native (localizes an ``s3://`` /
``hdfs://`` input and finalizes the output back) via the ``Storage`` abstraction,
mirroring ``pbf_geojson.convert_region``.

The Python-script predicate makes filtering as expressive as Python — far beyond
``key=value``. A script may either:

  * be a single boolean **expression** evaluated once per feature, with
    ``feature`` (the GeoJSON feature dict), ``props`` / ``tags`` (its
    ``properties`` dict), and ``geom`` (its ``geometry``) in scope, e.g.::

        props.get("amenity") == "charging_station" and (
            "tesla" in props.get("operator", "").lower()
            or "tesla" in props.get("brand", "").lower()
            or any(k.startswith("socket:tesla") for k in props)
        )

  * or define a ``def keep(feature):`` (or ``keep(props)``) function returning a
    truthy value to keep the feature::

        def keep(feature):
            p = feature["properties"]
            return p.get("amenity") == "charging_station" and "Tesla" in str(p)

Scripts run in a restricted namespace (a safe builtin subset plus ``re``/``math``)
— the same spirit as FFL ``script python`` blocks. No imports, file, or process
access; a misbehaving predicate that raises on a feature simply drops that
feature (and is counted) rather than aborting the whole filter.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from _osm_tools.storage import Storage, get_storage, local_staging_subdir

Predicate = Callable[[dict], bool]


@dataclass
class FilterResult:
    """Outcome of a :func:`filter_geojson` call."""

    output_path: str
    relative_path: str
    total: int
    kept: int
    errors: int


class FilterError(RuntimeError):
    """Raised when the filter cannot run (bad script, missing input, …)."""


# ---------------------------------------------------------------------------
# Predicate compilation
# ---------------------------------------------------------------------------

# A deliberately small, safe builtin surface for filter scripts. No open/eval/
# exec/import/__import__, no introspection helpers.
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "enumerate": enumerate,
    "filter": filter, "float": float, "int": int, "isinstance": isinstance, "len": len,
    "list": list, "map": map, "max": max, "min": min, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip, "range": range,
    "True": True, "False": False, "None": None,
}
_SAFE_GLOBALS: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS, "re": re, "math": math}


def compile_filter(predicate: str) -> Predicate:
    """Compile a filter ``predicate`` string into a ``keep(feature) -> bool``.

    ``predicate`` is interpreted as:

    * a Python script defining ``keep(...)`` — if ``"def keep"`` appears, OR
    * a single Python boolean **expression** over ``feature`` / ``props`` /
      ``tags`` / ``geom`` — otherwise.

    Compiled once; the returned callable is applied per feature. Raises
    :class:`FilterError` on a syntax error.
    """
    src = (predicate or "").strip()
    if not src:
        return lambda _f: True

    if "def keep" in src:
        ns: dict[str, Any] = dict(_SAFE_GLOBALS)
        try:
            exec(compile(src, "<filter-script>", "exec"), ns)  # noqa: S102 - sandboxed ns
        except SyntaxError as exc:
            raise FilterError(f"filter script syntax error: {exc}") from exc
        fn = ns.get("keep")
        if not callable(fn):
            raise FilterError("filter script must define a callable `keep(feature)`")

        def _run_keep(feature: dict) -> bool:
            return bool(fn(feature))

        return _run_keep

    # Treat as a boolean expression evaluated per feature.
    try:
        code = compile(src, "<filter-expr>", "eval")
    except SyntaxError as exc:
        raise FilterError(f"filter expression syntax error: {exc}") from exc

    def _run_expr(feature: dict) -> bool:
        props = feature.get("properties") or {}
        local = {
            "feature": feature,
            "props": props,
            "tags": props,
            "geom": feature.get("geometry") or {},
        }
        return bool(eval(code, _SAFE_GLOBALS, local))  # noqa: S307 - sandboxed globals

    return _run_expr


def tag_predicate(spec: str) -> Predicate:
    """Convenience: build a predicate from a simple ``a=b|c~d`` spec.

    ``key=value`` exact match, ``key~substr`` case-insensitive substring; ``|``
    is OR. Implemented on top of :func:`compile_filter` for one code path —
    callers wanting more than this pass a full script instead.
    """
    clauses: list[str] = []
    for term in (spec or "").split("|"):
        term = term.strip()
        if not term:
            continue
        if "~" in term:
            k, v = term.split("~", 1)
            clauses.append(f'{v.strip()!r}.lower() in str(props.get({k.strip()!r}, "")).lower()')
        elif "=" in term:
            k, v = term.split("=", 1)
            clauses.append(f'str(props.get({k.strip()!r}, "")) == {v.strip()!r}')
    expr = " or ".join(clauses) if clauses else "True"
    return compile_filter(expr)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_geojson(
    in_path: str,
    predicate: Predicate | str,
    out_path: str,
    *,
    storage: Any = None,
) -> FilterResult:
    """Filter the GeoJSON at ``in_path`` to ``out_path`` keeping features for
    which ``predicate`` is truthy.

    ``predicate`` may be a callable or a script/expression string (compiled via
    :func:`compile_filter`). Storage-agnostic: ``in_path``/``out_path`` may be
    local or ``s3://``/``hdfs://`` URIs — the input is localized and the output
    finalized through the backend, so the same call works against MinIO.
    """
    s = storage or get_storage()
    keep: Predicate = predicate if callable(predicate) else compile_filter(predicate)

    local_in = Path(s.localize(str(in_path)))
    with open(local_in, encoding="utf-8") as fh:
        fc = json.load(fh)
    features = fc.get("features") or []

    kept: list[dict] = []
    errors = 0
    for feat in features:
        try:
            if keep(feat):
                kept.append(feat)
        except Exception:  # noqa: BLE001 - a bad predicate drops the feature, not the run
            errors += 1

    out = {"type": "FeatureCollection", "features": kept}
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(out_path)).name)
    staging = Path(local_staging_subdir("facetwork-geojson-filter")) / f"{safe}.tmp"
    staging.parent.mkdir(parents=True, exist_ok=True)
    with open(staging, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    s.mkdir_p(Storage.dirname(str(out_path)))
    s.finalize_from_local(str(staging), str(out_path))

    return FilterResult(
        output_path=str(out_path),
        relative_path=Path(str(out_path)).name,
        total=len(features),
        kept=len(kept),
        errors=errors,
    )
