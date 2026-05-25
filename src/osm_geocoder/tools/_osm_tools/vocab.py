"""OSM tag vocabulary — the domain ontology behind the ``osm.Vocab`` facets.

The semantic half of the discovery layer: maps a natural-language term to the
OSM ``(key, value)`` tag it denotes, so a composer can turn "find a pharmacy"
into ``amenity=pharmacy`` deterministically (then feed that to
``ExtractCategory`` + ``FilterGeoJSONByOSMType``) instead of memorising tags.

The ontology is a curated, high-value subset — the tags a "find a place /
business" request actually reaches for — with synonyms (the real value-add:
"gas station" → ``amenity=fuel``, "grocery store" → ``shop=supermarket``,
"freeway" → ``highway=motorway``). It is intentionally not exhaustive; unknown
terms resolve to confidence 0 (an honest "no known tag"), and the table is easy
to extend. Pairs with the generic facet capability index (``fw_capabilities``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (key, value, [synonyms / natural-language terms]). The canonical value itself
# is always matchable; synonyms add the NL phrases people actually use.
_ONTOLOGY: list[tuple[str, str, list[str]]] = [
    # --- amenity: food & drink ---
    ("amenity", "restaurant", ["restaurant", "diner", "eatery", "bistro"]),
    ("amenity", "fast_food", ["fast food", "burger", "drive thru", "drive-through", "takeaway"]),
    ("amenity", "cafe", ["cafe", "coffee shop", "coffeehouse", "coffee", "espresso bar"]),
    ("amenity", "bar", ["bar", "cocktail bar", "tavern"]),
    ("amenity", "pub", ["pub", "brewpub"]),
    ("amenity", "ice_cream", ["ice cream", "gelato", "ice cream parlor"]),
    # --- amenity: health ---
    ("amenity", "pharmacy", ["pharmacy", "drugstore", "drug store", "chemist", "apothecary"]),
    ("amenity", "hospital", ["hospital", "emergency room", "er", "medical center"]),
    ("amenity", "clinic", ["clinic", "medical clinic", "urgent care"]),
    ("amenity", "doctors", ["doctor", "doctors", "physician", "gp", "doctor's office"]),
    ("amenity", "dentist", ["dentist", "dental", "dental office"]),
    ("amenity", "veterinary", ["vet", "veterinary", "animal hospital"]),
    # --- amenity: education ---
    ("amenity", "school", ["school", "elementary school", "primary school", "high school"]),
    ("amenity", "university", ["university"]),
    ("amenity", "college", ["college"]),
    ("amenity", "kindergarten", ["kindergarten", "preschool", "daycare", "nursery"]),
    ("amenity", "library", ["library"]),
    # --- amenity: finance ---
    ("amenity", "bank", ["bank"]),
    ("amenity", "atm", ["atm", "cash machine", "cashpoint"]),
    ("amenity", "bureau_de_change", ["currency exchange", "money exchange", "bureau de change"]),
    # --- amenity: transport / fuel ---
    ("amenity", "fuel", ["gas station", "gas", "petrol station", "petrol", "filling station", "service station"]),
    ("amenity", "charging_station", ["charging station", "ev charger", "ev charging", "electric vehicle charging"]),
    ("amenity", "parking", ["parking", "parking lot", "car park", "parking garage"]),
    ("amenity", "bicycle_parking", ["bike parking", "bicycle parking"]),
    ("amenity", "taxi", ["taxi", "taxi stand", "cab stand"]),
    # --- amenity: public / civic ---
    ("amenity", "police", ["police", "police station"]),
    ("amenity", "fire_station", ["fire station", "firehouse"]),
    ("amenity", "post_office", ["post office"]),
    ("amenity", "townhall", ["town hall", "city hall"]),
    ("amenity", "place_of_worship", ["church", "place of worship", "mosque", "temple", "synagogue"]),
    ("amenity", "toilets", ["toilet", "toilets", "restroom", "public restroom", "bathroom"]),
    ("amenity", "drinking_water", ["drinking water", "water fountain", "water tap"]),
    # --- amenity: leisure-ish ---
    ("amenity", "cinema", ["cinema", "movie theater", "movie theatre"]),
    ("amenity", "theatre", ["theatre", "theater", "playhouse"]),
    ("amenity", "nightclub", ["nightclub", "night club", "club"]),
    # --- shop ---
    ("shop", "supermarket", ["supermarket", "grocery store", "grocery", "groceries"]),
    ("shop", "convenience", ["convenience store", "corner store", "bodega", "mini mart"]),
    ("shop", "bakery", ["bakery", "baker"]),
    ("shop", "butcher", ["butcher", "butchers", "meat shop"]),
    ("shop", "greengrocer", ["greengrocer", "produce", "fruit and vegetable"]),
    ("shop", "mall", ["mall", "shopping mall", "shopping center", "shopping centre"]),
    ("shop", "department_store", ["department store"]),
    ("shop", "clothes", ["clothing store", "clothes", "apparel", "boutique"]),
    ("shop", "shoes", ["shoe store", "shoes"]),
    ("shop", "hardware", ["hardware store", "hardware"]),
    ("shop", "electronics", ["electronics store", "electronics"]),
    ("shop", "books", ["bookstore", "book shop", "books"]),
    ("shop", "florist", ["florist", "flower shop"]),
    ("shop", "hairdresser", ["hairdresser", "hair salon", "barber", "barbershop"]),
    ("shop", "car", ["car dealership", "car dealer", "auto dealer"]),
    ("shop", "car_repair", ["car repair", "auto repair", "mechanic", "garage"]),
    # --- highway ---
    ("highway", "motorway", ["motorway", "freeway", "interstate", "expressway", "highway"]),
    ("highway", "trunk", ["trunk road", "trunk"]),
    ("highway", "primary", ["primary road", "primary", "main road"]),
    ("highway", "secondary", ["secondary road", "secondary"]),
    ("highway", "tertiary", ["tertiary road", "tertiary"]),
    ("highway", "residential", ["residential street", "residential road", "residential"]),
    ("highway", "service", ["service road", "alley", "driveway"]),
    ("highway", "footway", ["sidewalk", "footpath", "footway", "pavement"]),
    ("highway", "cycleway", ["bike lane", "cycleway", "bike path", "cycle path"]),
    ("highway", "path", ["path", "trail"]),
    # --- tourism ---
    ("tourism", "hotel", ["hotel", "lodging", "accommodation"]),
    ("tourism", "motel", ["motel"]),
    ("tourism", "hostel", ["hostel"]),
    ("tourism", "guest_house", ["guest house", "guesthouse", "bed and breakfast", "b&b"]),
    ("tourism", "museum", ["museum"]),
    ("tourism", "attraction", ["attraction", "tourist attraction"]),
    ("tourism", "viewpoint", ["viewpoint", "scenic overlook", "lookout"]),
    # --- leisure ---
    ("leisure", "park", ["park", "public park"]),
    ("leisure", "playground", ["playground"]),
    ("leisure", "pitch", ["sports pitch", "playing field", "sports field"]),
    ("leisure", "sports_centre", ["sports centre", "sports center", "rec center", "recreation center"]),
    ("leisure", "fitness_centre", ["gym", "fitness center", "fitness centre", "health club"]),
    ("leisure", "swimming_pool", ["swimming pool", "pool"]),
    ("leisure", "garden", ["garden", "botanical garden"]),
    ("leisure", "golf_course", ["golf course", "golf"]),
    ("leisure", "stadium", ["stadium", "arena"]),
    # --- natural / landuse (areas) ---
    ("natural", "water", ["lake", "water", "pond", "reservoir"]),
    ("natural", "wood", ["forest", "wood", "woods"]),
    ("natural", "beach", ["beach"]),
    ("natural", "peak", ["peak", "mountain", "summit"]),
    ("landuse", "residential", ["residential area", "residential zone"]),
    ("landuse", "commercial", ["commercial area", "commercial zone"]),
    ("landuse", "industrial", ["industrial area", "industrial zone", "industrial park"]),
    ("landuse", "retail", ["retail area", "retail zone"]),
    ("landuse", "farmland", ["farmland", "farm", "agriculture", "cropland"]),
]


@dataclass
class TagMatch:
    """One resolved tag candidate."""

    key: str
    value: str
    confidence: float
    matched_term: str

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value,
                "confidence": round(self.confidence, 3), "matched_term": self.matched_term}


def _norm(text: str) -> str:
    """Lowercase, collapse separators/whitespace to single spaces."""
    return re.sub(r"[\s_\-]+", " ", (text or "").strip().lower()).strip()


# Precompute a normalized lookup: each entry's matchable terms (value + synonyms).
_ENTRIES = [
    (key, value, sorted({_norm(value)} | {_norm(s) for s in syns}))
    for key, value, syns in _ONTOLOGY
]


def keys() -> list[str]:
    """All tag keys the vocabulary covers (e.g. amenity, shop, highway)."""
    return sorted({key for key, _v, _t in _ENTRIES})


def list_values(key: str) -> list[str]:
    """All values known for a tag ``key`` (e.g. all amenity values)."""
    k = key.strip().lower()
    return sorted({value for ekey, value, _t in _ENTRIES if ekey == k})


def resolve(term: str, key: str = "") -> list[TagMatch]:
    """Resolve a natural-language ``term`` to ranked OSM ``(key, value)`` tags.

    Optionally constrain to a single tag ``key``. Match tiers (highest first):
    exact canonical value (1.0), exact synonym (0.9), token-subset (0.65),
    substring either direction (0.5). Returns de-duplicated matches sorted by
    confidence; an empty list means the term is not in the vocabulary.
    """
    norm = _norm(term)
    key_filter = key.strip().lower()
    if not norm:
        return []
    norm_tokens = set(norm.split())

    best: dict[tuple[str, str], TagMatch] = {}
    for ekey, value, terms in _ENTRIES:
        if key_filter and ekey != key_filter:
            continue
        nval = _norm(value)
        conf = 0.0
        matched = ""
        for t in terms:
            if norm == t:
                c = 1.0 if t == nval else 0.9
            elif norm_tokens and set(t.split()) and norm_tokens <= set(t.split()):
                c = 0.65
            elif norm in t or t in norm:
                c = 0.5
            else:
                c = 0.0
            if c > conf:
                conf, matched = c, t
        if conf <= 0.0:
            continue
        k = (ekey, value)
        if k not in best or conf > best[k].confidence:
            best[k] = TagMatch(key=ekey, value=value, confidence=conf, matched_term=matched)

    return sorted(best.values(), key=lambda m: (-m.confidence, m.key, m.value))
