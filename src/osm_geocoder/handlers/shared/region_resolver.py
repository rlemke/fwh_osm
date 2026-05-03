"""Region name resolver for OSM Geofabrik downloads.

Resolves human-friendly region names (e.g. "Colorado", "UK", "the Alps")
to Geofabrik download paths and cache facet names. Pure Python, no AFL
dependencies.

Uses the REGION_REGISTRY from cache_handlers.py as the authoritative source
of available regions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
