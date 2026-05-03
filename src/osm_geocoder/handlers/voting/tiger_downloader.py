"""US Census TIGER/Line shapefile downloader with filesystem caching.

Downloads electoral boundary shapefiles from the Census Bureau and caches them locally.
Supports Congressional Districts, State Legislative Districts, and Voting Precincts.
"""

import logging
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import requests

from facetwork.config import get_output_base

log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(get_output_base(), "census", "tiger-cache")
TIGER_BASE = "https://www2.census.gov/geo/tiger"
USER_AGENT = "Facetwork-Census-Example/1.0"

# District type identifiers
DISTRICT_CONGRESSIONAL = "cd"
DISTRICT_STATE_SENATE = "sldu"  # State Legislative District Upper
DISTRICT_STATE_HOUSE = "sldl"  # State Legislative District Lower
DISTRICT_VOTING_PRECINCT = "vtd"

# State FIPS codes
STATE_FIPS = {
    "AL": "01",
    "AK": "02",
    "AZ": "04",
    "AR": "05",
    "CA": "06",
    "CO": "08",
    "CT": "09",
    "DE": "10",
    "DC": "11",
    "FL": "12",
    "GA": "13",
    "HI": "15",
    "ID": "16",
    "IL": "17",
    "IN": "18",
    "IA": "19",
    "KS": "20",
    "KY": "21",
    "LA": "22",
    "ME": "23",
    "MD": "24",
    "MA": "25",
    "MI": "26",
    "MN": "27",
    "MS": "28",
    "MO": "29",
    "MT": "30",
    "NE": "31",
    "NV": "32",
    "NH": "33",
    "NJ": "34",
    "NM": "35",
    "NY": "36",
    "NC": "37",
    "ND": "38",
    "OH": "39",
    "OK": "40",
    "OR": "41",
    "PA": "42",
    "RI": "44",
    "SC": "45",
    "SD": "46",
    "TN": "47",
    "TX": "48",
    "UT": "49",
    "VT": "50",
    "VA": "51",
    "WA": "53",
    "WV": "54",
    "WI": "55",
    "WY": "56",
    "PR": "72",
    "VI": "78",
    "GU": "66",
    "AS": "60",
    "MP": "69",
}

# Reverse lookup: FIPS to state abbreviation
FIPS_TO_STATE = {v: k for k, v in STATE_FIPS.items()}

# Full state names to abbreviations
STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "puerto rico": "PR",
    "virgin islands": "VI",
    "guam": "GU",
    "american samoa": "AS",
    "northern mariana islands": "MP",
}


def _congress_for_year(year: int) -> int:
    """Derive the TIGER Congress number from a data year.

    TIGER files use the congress whose districts are in effect for elections
    in or after that year.  Known mappings from Census.gov:
    2020 → 116, 2021-2022 → 116, 2023 → 118, 2024 → 119.
    """
    # Known TIGER year → congress mappings
    _KNOWN = {2020: 116, 2021: 116, 2022: 116, 2023: 118, 2024: 119}
    if year in _KNOWN:
        return _KNOWN[year]
    # Extrapolate: odd years start a new congress
    # 119th started 2025, 120th starts 2027, etc.
    return 119 + (year - 2025) // 2


def resolve_state_fips(state: str) -> str:
    """Resolve a state name, abbreviation, or FIPS code to a FIPS code.

    Args:
        state: State name ("California"), abbreviation ("CA"), or FIPS ("06")

    Returns:
        2-digit FIPS code (e.g., "06")

    Raises:
        ValueError: If state cannot be resolved
    """
    state = state.strip()

    # Already a FIPS code?
    if len(state) == 2 and state.isdigit():
        if state in FIPS_TO_STATE:
            return state
        raise ValueError(f"Unknown FIPS code: {state}")

    # State abbreviation?
    upper = state.upper()
    if upper in STATE_FIPS:
        return STATE_FIPS[upper]

    # Full state name?
    lower = state.lower()
    if lower in STATE_NAMES:
        return STATE_FIPS[STATE_NAMES[lower]]

    raise ValueError(f"Unknown state: {state}")


def tiger_url(
    district_type: str, year: int, state_fips: str | None = None, congress_number: int | None = None
) -> str:
    """Build the TIGER/Line download URL for a district type.

    Args:
        district_type: One of cd, sldu, sldl, vtd
        year: Data year (e.g., 2023)
        state_fips: 2-digit state FIPS code (required for state-level data)
        congress_number: Congress number (required for Congressional Districts)

    Returns:
        Full download URL
    """
    year_str = str(year)
    base = f"{TIGER_BASE}/TIGER{year_str}"

    if district_type == DISTRICT_CONGRESSIONAL:
        if congress_number is None:
            congress_number = _congress_for_year(year)
        # Pre-2023: nationwide (tl_2020_us_cd116.zip)
        # 2023+: per-state (tl_2023_01_cd118.zip) — state_fips required
        if year < 2023:
            return f"{base}/CD/tl_{year_str}_us_cd{congress_number}.zip"
        if state_fips is None:
            raise ValueError("state_fips required for Congressional Districts (year >= 2023)")
        return f"{base}/CD/tl_{year_str}_{state_fips}_cd{congress_number}.zip"

    if district_type == DISTRICT_STATE_SENATE:
        if state_fips is None:
            raise ValueError("state_fips required for State Senate Districts")
        return f"{base}/SLDU/tl_{year_str}_{state_fips}_sldu.zip"

    if district_type == DISTRICT_STATE_HOUSE:
        if state_fips is None:
            raise ValueError("state_fips required for State House Districts")
        return f"{base}/SLDL/tl_{year_str}_{state_fips}_sldl.zip"

    if district_type == DISTRICT_VOTING_PRECINCT:
        if state_fips is None:
            raise ValueError("state_fips required for Voting Precincts")
        # VTD uses decennial census suffix (20 for 2020, 10 for 2010)
        suffix = "20" if year >= 2020 else "10"
        return f"{base}/VTD/tl_{year_str}_{state_fips}_vtd{suffix}.zip"

    raise ValueError(f"Unknown district type: {district_type}")


def cache_path(
    district_type: str, year: int, state_fips: str | None = None, congress_number: int | None = None
) -> str:
    """Build the local cache path for a district type."""
    year_str = str(year)

    if district_type == DISTRICT_CONGRESSIONAL:
        if congress_number is None:
            congress_number = _congress_for_year(year)
        if year < 2023 or state_fips is None:
            filename = f"tl_{year_str}_us_cd{congress_number}.zip"
        else:
            filename = f"tl_{year_str}_{state_fips}_cd{congress_number}.zip"
        subdir = "CD"
    elif district_type == DISTRICT_STATE_SENATE:
        filename = f"tl_{year_str}_{state_fips}_sldu.zip"
        subdir = f"SLDU/{state_fips}"
    elif district_type == DISTRICT_STATE_HOUSE:
        filename = f"tl_{year_str}_{state_fips}_sldl.zip"
        subdir = f"SLDL/{state_fips}"
    elif district_type == DISTRICT_VOTING_PRECINCT:
        suffix = "20" if year >= 2020 else "10"
        filename = f"tl_{year_str}_{state_fips}_vtd{suffix}.zip"
        subdir = f"VTD/{state_fips}"
    else:
        raise ValueError(f"Unknown district type: {district_type}")

    return os.path.join(CACHE_DIR, year_str, subdir, filename)


def download_tiger(
    district_type: str,
    year: int = 2023,
    state_fips: str | None = None,
    congress_number: int | None = None,
) -> dict:
    """Download a TIGER/Line shapefile, using local cache if available.

    Args:
        district_type: Type of district (cd, sldu, sldl, vtd)
        year: Data year
        state_fips: State FIPS code (for state-level data)
        congress_number: Congress number (for Congressional Districts)

    Returns:
        TIGERCache dict with url, path, date, size, wasInCache, year,
        district_type, and state_fips fields.

    Raises:
        requests.HTTPError: If download fails
        ValueError: If required parameters are missing
    """
    url = tiger_url(district_type, year, state_fips, congress_number)
    local_path = cache_path(district_type, year, state_fips, congress_number)

    if os.path.exists(local_path):
        size = os.path.getsize(local_path)
        log.info("Cache hit: %s", local_path)
        return {
            "url": url,
            "path": local_path,
            "date": datetime.now(UTC).isoformat(),
            "size": size,
            "wasInCache": True,
            "year": year,
            "district_type": district_type,
            "state": FIPS_TO_STATE.get(state_fips, state_fips) if state_fips else "US",
        }

    log.info("Downloading: %s", url)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    response = requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=300)
    response.raise_for_status()

    with open(local_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    size = os.path.getsize(local_path)
    log.info("Downloaded %d bytes to %s", size, local_path)

    return {
        "url": url,
        "path": local_path,
        "date": datetime.now(UTC).isoformat(),
        "size": size,
        "wasInCache": False,
        "year": year,
        "district_type": district_type,
        "state": FIPS_TO_STATE.get(state_fips, state_fips) if state_fips else "US",
    }


def extract_shapefile(zip_path: str, output_dir: str | None = None) -> str:
    """Extract a TIGER shapefile ZIP to a directory.

    Args:
        zip_path: Path to the ZIP file
        output_dir: Output directory (default: same directory as ZIP)

    Returns:
        Path to the extracted .shp file
    """
    if output_dir is None:
        output_dir = os.path.dirname(zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(output_dir)

    # Find the .shp file
    zip_dir = Path(output_dir)
    shp_files = list(zip_dir.glob("*.shp"))

    if not shp_files:
        raise FileNotFoundError(f"No .shp file found in {zip_path}")

    return str(shp_files[0])


def download_congressional_districts(
    year: int = 2024, congress_number: int | None = None, state_fips: str | None = None
) -> dict:
    """Download Congressional District boundaries.

    For year >= 2023, state_fips is required (files are per-state).
    congress_number is auto-derived from year if not specified.
    """
    if congress_number is None:
        congress_number = _congress_for_year(year)
    return download_tiger(
        DISTRICT_CONGRESSIONAL, year, state_fips=state_fips, congress_number=congress_number
    )


def download_state_senate_districts(state_fips: str, year: int = 2023) -> dict:
    """Download State Senate (Upper Chamber) district boundaries."""
    fips = resolve_state_fips(state_fips)
    return download_tiger(DISTRICT_STATE_SENATE, year, state_fips=fips)


def download_state_house_districts(state_fips: str, year: int = 2023) -> dict:
    """Download State House (Lower Chamber) district boundaries."""
    fips = resolve_state_fips(state_fips)
    return download_tiger(DISTRICT_STATE_HOUSE, year, state_fips=fips)


def download_voting_precincts(state_fips: str, year: int = 2020) -> dict:
    """Download Voting District/Precinct boundaries."""
    fips = resolve_state_fips(state_fips)
    return download_tiger(DISTRICT_VOTING_PRECINCT, year, state_fips=fips)
