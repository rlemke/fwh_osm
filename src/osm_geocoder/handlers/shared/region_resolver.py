"""Region name resolver for OSM Geofabrik downloads.

Resolves human-friendly region names (e.g. "Colorado", "UK", "the Alps")
to Geofabrik download paths and cache facet names. Pure Python, no AFL
dependencies.

Uses the REGION_REGISTRY from cache_handlers.py as the authoritative source
of available regions.

Two resolver surfaces coexist:

- The legacy ``resolve()`` / ``list_regions()`` / ``list_geographic_features()``
  trio, returning ``ResolutionResult`` + ``RegionMatch`` records. These back
  the legacy ``osm.Region.ResolveRegion`` event facet.
- The newer ``resolve_batch()`` / ``list_regions_typed()`` returning ``Region``
  records (matching the FFL ``osm.types.Region`` schema). These back the new
  ``ResolveRegions(names: [String])`` and ``ListRegions(parent_canonical,
  level, continent)`` facets. Use these for new callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..cache.cache_handlers import REGION_REGISTRY


@dataclass(frozen=True)
class RegionMatch:
    """A resolved region with its Geofabrik metadata."""

    namespace: str
    facet_name: str
    geofabrik_path: str

    @property
    def continent(self) -> str:
        """Extract continent from namespace (e.g. 'Africa' from 'osm.cache.Africa')."""
        return self.namespace.rsplit(".", 1)[-1]

    @property
    def qualified_name(self) -> str:
        """Full qualified facet name (e.g. 'osm.cache.Africa.Algeria')."""
        return f"{self.namespace}.{self.facet_name}"


@dataclass
class ResolutionResult:
    """Result of resolving a region name."""

    matches: list[RegionMatch]
    query: str
    is_ambiguous: bool = False
    is_geographic_feature: bool = False
    disambiguation: str = ""


@dataclass(frozen=True)
class Region:
    """Canonical identity for an OSM extract — Python mirror of the FFL
    ``osm.types.Region`` schema.

    Distinct from ``RegionMatch`` (which is a thin record over the legacy
    ``REGION_REGISTRY`` rows): ``Region`` adds derived hierarchy fields
    (``level``, ``level_label``, ``parent_canonical``) and the original
    user input (``query``) so downstream handlers can render diagnostics
    without re-resolving.
    """

    query: str
    name: str
    canonical: str
    level: str
    level_label: str
    parent_canonical: str
    continent: str
    geofabrik_path: str

    def to_dict(self) -> dict:
        """Shape that matches the FFL Region schema field-for-field."""
        return {
            "query": self.query,
            "name": self.name,
            "canonical": self.canonical,
            "level": self.level,
            "level_label": self.level_label,
            "parent_canonical": self.parent_canonical,
            "continent": self.continent,
            "geofabrik_path": self.geofabrik_path,
        }


@dataclass(frozen=True)
class AmbiguousMatch:
    """One ambiguous resolution kept for caller review."""

    query: str
    chosen: Region
    candidates: list[Region]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "chosen": self.chosen.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass(frozen=True)
class FeatureExpansion:
    """A name that expanded into multiple regions (e.g. 'Alps' → 7 countries)."""

    query: str
    feature_name: str
    regions: list[Region]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "feature_name": self.feature_name,
            "regions": [r.to_dict() for r in self.regions],
        }


@dataclass
class ResolutionDiagnostics:
    """Side-channel report from ``resolve_batch``."""

    unresolved: list[str] = field(default_factory=list)
    ambiguous: list[AmbiguousMatch] = field(default_factory=list)
    expanded: list[FeatureExpansion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "unresolved": list(self.unresolved),
            "ambiguous": [a.to_dict() for a in self.ambiguous],
            "expanded": [e.to_dict() for e in self.expanded],
        }


@dataclass
class BatchResolution:
    """Result of ``resolve_batch``."""

    regions: list[Region]
    diagnostics: ResolutionDiagnostics

    def to_dict(self) -> dict:
        return {
            "regions": [r.to_dict() for r in self.regions],
            "diagnostics": self.diagnostics.to_dict(),
        }


class StrictResolutionError(ValueError):
    """Raised by ``resolve_batch(..., strict=True)`` when any name fails to resolve."""


def _normalize(name: str) -> str:
    """Normalize a name for lookup: lowercase, strip, remove hyphens/underscores, strip leading 'the'."""
    s = name.strip().lower()
    s = re.sub(r"[-_\s]+", "", s)
    if s.startswith("the"):
        s = s[3:]
    return s


# Alias mappings: normalized alternate name -> normalized canonical facet name
ALIASES: dict[str, str] = {
    # Country abbreviations
    "uk": "unitedkingdom",
    "gb": "unitedkingdom",
    "greatbritain": "unitedkingdom",
    "britain": "unitedkingdom",
    "england": "unitedkingdom",
    "usa": "unitedstates",
    "us": "unitedstates",
    "america": "unitedstates",
    "uae": "unitedarabemirates",
    "drc": "congokinshasa",
    "congo": "congobrazzaville",
    "czechia": "czechrepublic",
    "czech": "czechrepublic",
    "holland": "netherlands",
    "burma": "myanmar",
    "swaziland": "eswatini",
    "persia": "iran",
    "northmacedonia": "macedonia",
    "timor": "easttimor",
    "timorleste": "easttimor",
    "palestine": "israelandpalestine",
    "israel": "israelandpalestine",
    "dc": "districtofcolumbia",
    "washingtondc": "districtofcolumbia",
    "png": "papuanewguinea",
    "nz": "newzealand",
    "aotearoa": "newzealand",
    "oceania": "allaustralia",
    "australiaoceania": "allaustralia",
    # US state 2-letter postal codes
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "newhampshire",
    "nj": "newjersey",
    "nm": "newmexico",
    "ny": "newyork",
    "nc": "northcarolina",
    "nd": "northdakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhodeisland",
    "sc": "southcarolina",
    "sd": "southdakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "westvirginia",
    "wi": "wisconsin",
    "wy": "wyoming",
    # Canadian province abbreviations
    "bc": "britishcolumbia",
    "ab": "alberta",
    "mb": "manitoba",
    "nb": "newbrunswick",
    "nl": "newfoundlandandlabrador",
    "ns": "novascotia",
    "on": "ontario",
    "pe": "princeedwardisland",
    "qc": "quebec",
    "sk": "saskatchewan",
    "yk": "yukon",
    "pei": "princeedwardisland",
}

# Geographic features: normalized feature name -> list of normalized region names
GEOGRAPHIC_FEATURES: dict[str, list[str]] = {
    # Mountain ranges
    "alps": ["austria", "switzerland", "france", "italy", "germany", "slovenia", "liechtenstein"],
    "rockies": [
        "colorado",
        "montana",
        "wyoming",
        "idaho",
        "utah",
        "newmexico",
        "alberta",
        "britishcolumbia",
    ],
    "andes": ["argentina", "chile", "peru", "bolivia", "ecuador", "colombia"],
    "himalayas": ["nepal", "india", "bhutan", "china", "pakistan"],
    "pyrenees": ["france", "spain", "andorra"],
    "carpathians": [
        "romania",
        "ukraine",
        "poland",
        "slovakia",
        "czechrepublic",
        "hungary",
        "serbia",
    ],
    "appalachians": [
        "virginia",
        "westvirginia",
        "northcarolina",
        "tennessee",
        "kentucky",
        "georgia",
        "pennsylvania",
        "newyork",
        "vermont",
        "newhampshire",
        "maine",
        "maryland",
    ],
    "cascades": ["washington", "oregon"],
    "sierranevada": ["california", "nevada"],
    # Named regions
    "scandinavia": ["norway", "sweden", "denmark", "finland", "iceland"],
    "baltics": ["estonia", "latvia", "lithuania"],
    "balkans": [
        "albania",
        "bosniaandherzegovina",
        "bulgaria",
        "croatia",
        "kosovo",
        "macedonia",
        "montenegro",
        "serbia",
        "slovenia",
        "greece",
        "romania",
    ],
    "benelux": ["belgium", "netherlands", "luxembourg"],
    "iberia": ["spain", "portugal"],
    "dach": ["germany", "austria", "switzerland"],
    "middleeast": [
        "iran",
        "iraq",
        "jordan",
        "lebanon",
        "syria",
        "yemen",
        "israelandpalestine",
        "saudiarabia",
    ],
    "southeastasia": [
        "cambodia",
        "indonesia",
        "laos",
        "malaysia",
        "myanmar",
        "philippines",
        "thailand",
        "vietnam",
        "brunei",
        "easttimor",
        "singapore",
    ],
    "newengland": [
        "connecticut",
        "maine",
        "massachusetts",
        "newhampshire",
        "rhodeisland",
        "vermont",
    ],
    "pacificnorthwest": ["washington", "oregon", "britishcolumbia"],
    "greatlakes": ["michigan", "wisconsin", "minnesota", "illinois", "indiana", "ohio", "ontario"],
    "deepsouth": ["alabama", "mississippi", "louisiana", "georgia", "southcarolina"],
    "greatplains": ["kansas", "nebraska", "southdakota", "northdakota", "oklahoma"],
    "tristate": ["newyork", "newjersey", "connecticut"],
    "eastafrica": ["kenya", "tanzania", "uganda", "rwanda", "burundi", "ethiopia"],
    "westafrica": [
        "nigeria",
        "ghana",
        "senegal",
        "mali",
        "guineabissau",
        "guinea",
        "sierraleone",
        "liberia",
        "burkinafaso",
        "togo",
        "benin",
        "niger",
        "gambia",
        "capeverde",
    ],
    "northafrica": ["morocco", "algeria", "tunisia", "libya", "egypt"],
    "southernafrica": [
        "southafrica",
        "namibia",
        "botswana",
        "zimbabwe",
        "mozambique",
        "zambia",
        "malawi",
        "lesotho",
        "eswatini",
    ],
    "hornofafrica": ["ethiopia", "eritrea", "somalia", "djibouti"],
    "patagonia": ["argentina", "chile"],
}

# Internal lookup index: normalized name -> list[RegionMatch]
_LOOKUP: dict[str, list[RegionMatch]] = {}
_INDEX_BUILT = False


def _build_index() -> None:
    """Build the lookup index from the REGION_REGISTRY."""
    global _LOOKUP, _INDEX_BUILT
    if _INDEX_BUILT:
        return

    _LOOKUP = {}

    for namespace, facets in REGION_REGISTRY.items():
        for facet_name, geofabrik_path in facets.items():
            match = RegionMatch(
                namespace=namespace,
                facet_name=facet_name,
                geofabrik_path=geofabrik_path,
            )

            # Index by normalized facet name
            norm_name = _normalize(facet_name)
            _LOOKUP.setdefault(norm_name, []).append(match)

            # Also index by Geofabrik path leaf segment
            # e.g. "africa/south-africa" -> "southafrica"
            leaf = geofabrik_path.rsplit("/", 1)[-1]
            norm_leaf = _normalize(leaf)
            if norm_leaf != norm_name:
                _LOOKUP.setdefault(norm_leaf, []).append(match)

    _INDEX_BUILT = True


def _deduplicate(matches: list[RegionMatch]) -> list[RegionMatch]:
    """Remove duplicates by geofabrik_path, keeping first occurrence."""
    seen: set[str] = set()
    result = []
    for m in matches:
        if m.geofabrik_path not in seen:
            seen.add(m.geofabrik_path)
            result.append(m)
    return result


def resolve(name: str, prefer_continent: str | None = None) -> ResolutionResult:
    """Resolve a human-friendly region name to Geofabrik download paths.

    Args:
        name: Region name (e.g. "Colorado", "UK", "the Alps", "Czech Republic").
        prefer_continent: Optional continent to disambiguate (e.g. "NorthAmerica"
            for "Georgia" the US state vs "Europe" for the country).

    Returns:
        ResolutionResult with matching regions.
    """
    _build_index()

    norm = _normalize(name)

    if not norm:
        return ResolutionResult(matches=[], query=name)

    # 1. Check geographic features first
    if norm in GEOGRAPHIC_FEATURES:
        constituent_names = GEOGRAPHIC_FEATURES[norm]
        all_matches: list[RegionMatch] = []
        for region_name in constituent_names:
            # Resolve each constituent through the same pipeline
            resolved_name = ALIASES.get(region_name, region_name)
            if resolved_name in _LOOKUP:
                all_matches.extend(_LOOKUP[resolved_name])
        all_matches = _deduplicate(all_matches)
        if prefer_continent:
            norm_continent = _normalize(prefer_continent)
            filtered = [m for m in all_matches if _normalize(m.continent) == norm_continent]
            if filtered:
                all_matches = filtered
        return ResolutionResult(
            matches=all_matches,
            query=name,
            is_geographic_feature=True,
        )

    # 2. Check aliases
    if norm in ALIASES:
        norm = ALIASES[norm]

    # 3. Direct lookup
    matches = list(_LOOKUP.get(norm, []))
    matches = _deduplicate(matches)

    # 4. Apply continent preference for disambiguation
    is_ambiguous = False
    disambiguation = ""
    if len(matches) > 1 and prefer_continent:
        norm_continent = _normalize(prefer_continent)
        filtered = [m for m in matches if _normalize(m.continent) == norm_continent]
        if filtered:
            matches = filtered
    elif len(matches) > 1:
        # Check if matches span multiple continents (true ambiguity)
        _continents = {m.continent for m in matches}
        # Filter out "Continents" namespace entries for ambiguity check
        non_continent_matches = [m for m in matches if m.namespace != "osm.cache.Continents"]
        non_continent_continents = {m.continent for m in non_continent_matches}
        if len(non_continent_continents) > 1:
            is_ambiguous = True
            disambiguation = (
                f"'{name}' matches regions in: "
                + ", ".join(sorted(non_continent_continents))
                + ". Use prefer_continent to disambiguate."
            )

    return ResolutionResult(
        matches=matches,
        query=name,
        is_ambiguous=is_ambiguous,
        disambiguation=disambiguation,
    )


def list_regions(continent: str | None = None) -> list[RegionMatch]:
    """List all available regions, optionally filtered by continent.

    Args:
        continent: Optional continent name to filter by (e.g. "Europe", "Africa").

    Returns:
        List of RegionMatch objects.
    """
    _build_index()

    all_matches: list[RegionMatch] = []
    for matches in _LOOKUP.values():
        all_matches.extend(matches)
    all_matches = _deduplicate(all_matches)

    if continent:
        norm_continent = _normalize(continent)
        all_matches = [m for m in all_matches if _normalize(m.continent) == norm_continent]

    return sorted(all_matches, key=lambda m: (m.continent, m.facet_name))


def list_geographic_features() -> dict[str, list[str]]:
    """List all recognized geographic features and their constituent regions.

    Returns:
        Dict mapping feature name to list of region names.
    """
    return dict(GEOGRAPHIC_FEATURES)


# ---------------------------------------------------------------------------
# Region-typed surface (new) — backs ResolveRegions / ListRegions FFL facets.
# ---------------------------------------------------------------------------

# Geofabrik top-level continent slugs → display continent name used in
# Region.continent. Top-level extracts without a continent parent (Antarctica,
# Russia, Planet) map to "".
_CONTINENT_DISPLAY: dict[str, str] = {
    "africa": "Africa",
    "asia": "Asia",
    "europe": "Europe",
    "north-america": "NorthAmerica",
    "central-america": "CentralAmerica",
    "south-america": "SouthAmerica",
    "australia-oceania": "Australia",
}

# Level keywords accepted as qualifier suffixes (parenthetical form).
_LEVEL_KEYWORDS = {"planet", "continent", "country", "subnational", "state", "province", "territory", "feature"}

# Known Canadian territories (the registry currently lists only Yukon —
# NWT and Nunavut are absent from Geofabrik's Canada tree).
_CANADIAN_TERRITORIES = {"yukon", "northwest-territories", "nunavut"}


def _level_from_path(path: str) -> str:
    """Derive Region.level from a Geofabrik path.

    Path shape determines level deterministically:
        "planet"                                  → "planet"
        "africa", "europe", "russia"              → "continent"
        "africa/algeria", "north-america/us"      → "country"
        "north-america/us/california"             → "subnational"
    """
    if path == "planet":
        return "planet"
    if "/" not in path:
        return "continent"
    if path.count("/") == 1:
        return "country"
    return "subnational"


def _level_label_from_path(path: str) -> str:
    """Geofabrik's human term for the extract — for display only.

    The closed-set ``level`` answers 'how should this filter?'; ``level_label``
    answers 'how should I show this to a person?'. They differ for subnational
    extracts where Geofabrik uses 'state' / 'province' / 'territory' / 'Land' /
    'constituent country' depending on country.
    """
    level = _level_from_path(path)
    if level != "subnational":
        return level if level != "planet" else ""

    # Subnational specialization by country path
    if path.startswith("north-america/us/"):
        return "state"
    if path.startswith("north-america/canada/"):
        leaf = path.rsplit("/", 1)[-1]
        if leaf in _CANADIAN_TERRITORIES:
            return "territory"
        return "province"
    # Future: europe/germany/<land>, europe/great-britain/<country>, etc.
    return "subnational"


def _continent_from_path(path: str) -> str:
    """Derive Region.continent from a Geofabrik path.

    Follows Geofabrik's tree placement. Top-level extracts (continents
    themselves, plus Russia / Antarctica / Planet) return "" — they are
    *not* members of a continent under Geofabrik's organization.
    """
    if "/" not in path:
        return ""
    first = path.split("/", 1)[0]
    return _CONTINENT_DISPLAY.get(first, "")


def _parent_canonical_from_path(path: str) -> str:
    """Derive Region.parent_canonical from a Geofabrik path.

    The parent is one path segment up. Continents and Planet have no parent
    in Geofabrik's tree and return "".
    """
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _display_name_from_facet(facet_name: str, geofabrik_path: str) -> str:
    """Render a friendly display name for the matched extract.

    Strips registry-internal naming artifacts so callers see human-readable
    names rather than the registry's internal facet keys:

        "BritishColumbia"          → "British Columbia"
        "Congo_Brazzaville"        → "Congo Brazzaville"
        "GeorgiaUS" / "GeorgiaEU"  → "Georgia"
        "DistrictOfColumbia"       → "District Of Columbia"
        "AllAfrica"                → "Africa"        (registry-internal alias)
        "AllUnitedStates"          → "United States" (registry-internal alias)
    """
    name = facet_name
    # Strip "All<X>" registry pseudonyms — these are aliases for the same
    # path as the non-All entry (e.g. AllAfrica → africa, Africa → africa).
    if name.startswith("All") and len(name) > 3 and name[3].isupper():
        name = name[3:]
    # Strip the disambiguator we added in the registry for the two Georgias.
    if name in ("GeorgiaUS", "GeorgiaEU"):
        name = "Georgia"
    # Replace underscores with spaces.
    name = name.replace("_", " ")
    # PascalCase → "Pascal Case", but preserve already-spaced segments.
    parts = []
    for segment in name.split(" "):
        # Insert a space before each capital that follows a lowercase letter.
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", segment)
        parts.append(spaced)
    return " ".join(parts).strip()


def _match_to_region(match: RegionMatch, query: str) -> Region:
    """Convert a legacy RegionMatch into a typed Region record."""
    path = match.geofabrik_path
    return Region(
        query=query,
        name=_display_name_from_facet(match.facet_name, path),
        canonical=path,
        level=_level_from_path(path),
        level_label=_level_label_from_path(path),
        parent_canonical=_parent_canonical_from_path(path),
        continent=_continent_from_path(path),
        geofabrik_path=path,
    )


def _parse_qualifier_suffix(name: str) -> tuple[str, str | None]:
    """Strip a qualifier suffix from a user-supplied region name.

    Accepts two forms (whichever matches first):
        "Georgia, US"          → ("Georgia", "US")
        "Córdoba, ES"          → ("Córdoba", "ES")
        "Georgia (country)"    → ("Georgia", "country")
        "Quebec  (province)"   → ("Quebec", "province")
        "Georgia"              → ("Georgia", None)

    A canonical path like "north-america/us/georgia" passes through
    unchanged — the slash makes the parser treat it as a single token
    and the caller's canonical-path branch picks it up.
    """
    s = name.strip()
    # Canonical paths bypass qualifier parsing.
    if "/" in s:
        return s, None

    # Parenthetical form: "Georgia (country)"
    m = re.match(r"^(.+?)\s*\(\s*([^)]+?)\s*\)\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Comma form: "Georgia, US" — only treat as qualifier if the tail looks
    # like a single short token, not a multi-word phrase that's part of the
    # region name itself.
    if "," in s:
        head, _, tail = s.rpartition(",")
        head = head.strip()
        tail = tail.strip()
        # Heuristic: a qualifier is short (≤ 20 chars) and a single token
        # OR a recognized level keyword / continent name. Multi-word region
        # names with embedded commas are not common in Geofabrik's tree.
        if head and tail and len(tail) <= 20 and " " not in tail:
            return head, tail
    return s, None


def _match_canonical_path(path: str) -> RegionMatch | None:
    """If ``path`` is a known Geofabrik path, return the matching RegionMatch."""
    _build_index()
    normpath = path.strip().lower()
    for matches in _LOOKUP.values():
        for m in matches:
            if m.geofabrik_path == normpath:
                return m
    return None


def _apply_qualifier(
    matches: list[RegionMatch], qualifier: str
) -> list[RegionMatch]:
    """Filter a candidate match list by a parsed qualifier hint.

    The qualifier may be:
      - A level keyword ("country", "state", "province", "subnational", ...) —
        filter by ``_level_from_path`` / ``_level_label_from_path``.
      - A continent name ("Europe", "NorthAmerica") — filter by continent
        (matches the legacy ``prefer_continent`` semantics).
      - A country / parent canonical alias ("US", "Canada", "Spain") — resolve
        the qualifier itself, then keep only matches whose path is at or under
        the qualifier's resolved path.

    Returns the filtered list, or the original list if the qualifier produced
    no matches (caller decides whether to mark ambiguous / unresolved).
    """
    norm_q = _normalize(qualifier)
    if not norm_q:
        return matches

    # 1. Level keyword filter
    if norm_q in _LEVEL_KEYWORDS:
        filtered = [
            m
            for m in matches
            if _level_from_path(m.geofabrik_path) == norm_q
            or _level_label_from_path(m.geofabrik_path) == norm_q
        ]
        if filtered:
            return filtered

    # 2. Continent filter (display names like "Europe", "NorthAmerica" plus
    #    Geofabrik slugs like "north-america")
    continent_candidates = {v.lower() for v in _CONTINENT_DISPLAY.values()}
    if norm_q in continent_candidates or norm_q in _CONTINENT_DISPLAY:
        # Normalize to display form: "northamerica" / "north-america" both
        # match continent display "NorthAmerica".
        display_target = None
        for slug, disp in _CONTINENT_DISPLAY.items():
            if _normalize(disp) == norm_q or slug == norm_q:
                display_target = disp
                break
        if display_target:
            filtered = [
                m
                for m in matches
                if _continent_from_path(m.geofabrik_path) == display_target
            ]
            if filtered:
                return filtered

    # 3. Country / parent path filter — resolve the qualifier itself.
    q_resolved = ALIASES.get(norm_q, norm_q)
    q_matches = _LOOKUP.get(q_resolved, [])
    if q_matches:
        # Use the first match's path as the parent prefix. If the qualifier
        # resolves ambiguously, prefer one whose path matches a candidate's
        # parent — e.g. qualifier "US" should resolve to "north-america/us".
        q_paths = {m.geofabrik_path for m in q_matches}
        filtered = [
            m
            for m in matches
            if m.geofabrik_path in q_paths
            or any(m.geofabrik_path.startswith(qp + "/") for qp in q_paths)
        ]
        if filtered:
            return filtered

    return matches


def _resolve_one_to_region(
    name: str, prefer_continent: str | None
) -> tuple[Region | None, list[Region], FeatureExpansion | None, bool]:
    """Internal: resolve a single name into one Region (best match) plus
    candidates and an optional FeatureExpansion record.

    Returns ``(best_region, candidates, expansion, is_ambiguous)``.
    - ``best_region`` is None when nothing resolved.
    - ``candidates`` lists every match as a Region (best first); for
      unambiguous resolution this is a single-element list. For features
      it is the expanded set.
    - ``expansion`` is set when the name resolved to a multi-region feature.
    - ``is_ambiguous`` is True when multiple candidates remain across
      different continents after qualifier + prefer_continent filtering.

    Unlike legacy ``resolve()``, the new API uses path-based continent
    inference (``_continent_from_path``) for filtering, so callers can
    say ``prefer_continent="NorthAmerica"`` and have it match the US-state
    extracts that the legacy code keyed under namespace "UnitedStates".
    """
    # Canonical path passthrough (skip qualifier parsing and lookup).
    if "/" in name or name.strip().lower() == "planet":
        match = _match_canonical_path(name.strip().lower())
        if match:
            region = _match_to_region(match, query=name)
            return region, [region], None, False

    base_name, qualifier = _parse_qualifier_suffix(name)
    # Resolve WITHOUT continent filtering — apply it ourselves below using
    # the new path-based logic so callers can use Geofabrik display names
    # ("NorthAmerica") rather than legacy namespace-last-segment values
    # ("UnitedStates"). Qualifier takes precedence over batch prefer_continent.
    result = resolve(base_name, prefer_continent=None)

    if not result.matches:
        return None, [], None, False

    matches = result.matches

    # Feature expansion path: surface the expansion record but still flatten
    # the constituents into the regions list (the agreed design).
    expansion: FeatureExpansion | None = None
    if result.is_geographic_feature:
        regions = [_match_to_region(m, query=name) for m in matches]
        expansion = FeatureExpansion(
            query=name,
            feature_name=_normalize(base_name),
            regions=regions,
        )
        return regions[0], regions, expansion, False

    # Apply the effective hint (qualifier > batch prefer_continent) via
    # _apply_qualifier, which handles continent / level / parent-path filters
    # using the new path-based inference.
    effective_hint = qualifier if qualifier else prefer_continent
    if effective_hint:
        refined = _apply_qualifier(matches, effective_hint)
        if refined:
            matches = refined

    candidates = [_match_to_region(m, query=name) for m in matches]
    # Ambiguity: still multiple candidates spanning different continents.
    is_ambiguous = (
        len(candidates) > 1
        and len({c.continent for c in candidates if c.continent}) > 1
    )
    return candidates[0], candidates, expansion, is_ambiguous


def resolve_batch(
    names: list[str],
    prefer_continent: str | None = None,
    strict: bool = False,
) -> BatchResolution:
    """Resolve a heterogeneous list of region names to ``Region`` records.

    Continents, countries, subnational regions, and named geographic
    features (``"Alps"``, ``"Scandinavia"``) may all appear in ``names``.

    Per-name disambiguation via qualifier suffix takes precedence over
    the batch-wide ``prefer_continent`` for that name only:
        "Georgia, US"            → US state
        "Georgia (country)"      → country
        "north-america/us/georgia" — canonical-path passthrough

    Args:
        names: List of human-friendly region names (or canonical paths).
        prefer_continent: Batch-wide tiebreaker for names that did not
            carry a qualifier suffix.
        strict: When True, raises ``StrictResolutionError`` if any name
            is unresolved or remains ambiguous after both the qualifier
            and ``prefer_continent`` are applied. When False (default),
            partial results are returned and the caller inspects
            ``diagnostics``.

    Returns:
        ``BatchResolution`` with the resolved regions (flat list — feature
        expansions contribute every constituent) and a diagnostics record.
    """
    _build_index()

    regions: list[Region] = []
    diagnostics = ResolutionDiagnostics()
    seen_paths: set[str] = set()

    for raw in names:
        if not raw or not raw.strip():
            diagnostics.unresolved.append(raw)
            continue

        best, candidates, expansion, is_ambiguous = _resolve_one_to_region(
            raw, prefer_continent
        )

        if best is None:
            diagnostics.unresolved.append(raw)
            continue

        if is_ambiguous:
            diagnostics.ambiguous.append(
                AmbiguousMatch(query=raw, chosen=best, candidates=candidates)
            )

        if expansion is not None:
            diagnostics.expanded.append(expansion)
            # Feature expansions: every constituent contributes to the flat list.
            for r in candidates:
                if r.canonical not in seen_paths:
                    seen_paths.add(r.canonical)
                    regions.append(r)
        else:
            if best.canonical not in seen_paths:
                seen_paths.add(best.canonical)
                regions.append(best)

    if strict and (diagnostics.unresolved or diagnostics.ambiguous):
        problems: list[str] = []
        if diagnostics.unresolved:
            problems.append(f"unresolved: {diagnostics.unresolved}")
        if diagnostics.ambiguous:
            ambig_summary = [a.query for a in diagnostics.ambiguous]
            problems.append(f"ambiguous: {ambig_summary}")
        raise StrictResolutionError("; ".join(problems))

    return BatchResolution(regions=regions, diagnostics=diagnostics)


def list_regions_typed(
    parent_canonical: str | None = None,
    level: str | None = None,
    continent: str | None = None,
) -> list[Region]:
    """List the known region catalog as ``Region`` records.

    All filters are optional and combine with AND semantics:
        list_regions_typed(level="continent")
            → continents + Planet
        list_regions_typed(parent_canonical="north-america/canada")
            → all Canadian provinces + territories
        list_regions_typed(level="subnational", continent="NorthAmerica")
            → US states + Canadian provinces / territories
    """
    _build_index()

    all_matches: list[RegionMatch] = []
    for matches in _LOOKUP.values():
        all_matches.extend(matches)
    all_matches = _deduplicate(all_matches)

    regions = [
        _match_to_region(m, query="")
        for m in sorted(all_matches, key=lambda m: m.geofabrik_path)
    ]

    if parent_canonical:
        target = parent_canonical.strip().lower()
        regions = [r for r in regions if r.parent_canonical == target]
    if level:
        target_level = level.strip().lower()
        regions = [r for r in regions if r.level == target_level]
    if continent:
        norm_target = _normalize(continent)
        regions = [r for r in regions if _normalize(r.continent) == norm_target]

    return regions
