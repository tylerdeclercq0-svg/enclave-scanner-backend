"""
Ring demographics — 5/10/15-mile Census ACS lookups around a candidate
parcel, triggered ON DEMAND ONLY.

Per explicit product requirement: this must NEVER run during the
initial county scan. It is deliberately a secondary, opt-in action a
user takes on a specific parcel they've already decided is worth a
closer look — not something that runs across every candidate returned
by a scan. Calling this for every parcel in a 50-parcel scan result
would mean 50 x (however many block groups intersect 3 rings each)
API calls, which is slow, wasteful, and defeats the point of a fast
initial filter.

Data source: U.S. Census Bureau ACS 5-Year Detailed Tables, block group
level (the finest geography ACS detailed tables support — roughly
600-3,000 people per block group, small enough for meaningful 5/10/15
mile rings without being so fine-grained that margins of error swamp
the estimate). Confirmed live, free, requires an API key tied to an
email address (.gov/.edu/.com/.org/.net) via
https://api.census.gov/data/key_signup.html.

Boundary geometry comes from the Census Bureau's TIGERweb REST service
(block group polygons by state/county/tract), a separate free service
from the ACS statistical API itself.

Methodology: weighted block-centroid apportionment, the same approach
Esri's GeoEnrichment service uses for ring/radius demographic queries.
Block groups fully inside a ring count fully; block groups only
partially inside are apportioned by what fraction of their internal
census blocks (finer than block groups) fall inside the ring. NOTE:
the partial-apportionment half of that is not implemented yet — see
the inline comment in compute_ring_demographics() below; this is
tracked as a known simplification, not silently skipped.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional

try:
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from encirclement import esri_json_to_shapely
except ImportError:  # pragma: no cover
    Point = unary_union = esri_json_to_shapely = None  # type: ignore

import requests


CENSUS_ACS5_BASE = "https://api.census.gov/data/2023/acs/acs5"
# Baseline vintage for the population-trend comparison. 2018 vs 2023 =
# 5-year gap. Confirmed via Phase B probe that the 2018 endpoint works
# and returns real numbers -- BUT block-group boundaries were redrawn
# between the 2010 Census (used by ACS 2018 5-year) and the 2020 Census
# (used by ACS 2023 5-year), so per-BG comparisons are silently
# misleading. County FIPS are stable, so a county-level 2018 vs 2023
# comparison is the ONLY reliable trend. Ring-level trend is offered as
# a secondary "directional-only" number with an explicit boundary-
# redraw flag; see fetch_population_trend below.
CENSUS_ACS5_TREND_BASELINE_YEAR = 2018
CENSUS_ACS5_CURRENT_YEAR = 2023
# Layer 10 ("Census Block Groups") of the ACS2023 vintage MapServer —
# confirmed live via describe_layer against the real service 2026-07-04.
# The previously-used "TIGERweb/State_County/MapServer" service has NO
# block group layer at any index (only States/Counties) — that was a
# real bug, not just an unconfirmed layer index, and silently returned
# zero features for every query regardless of point/radius.
TIGERWEB_BLOCKGROUP_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/tigerWMS_ACS2023/MapServer/10"
)

# ACS variables pulled per block group. Split into 4 groups because the
# Census ACS API caps a single request at 50 variables and our full set
# now hits ~70 (per Phase B code-table confirmation, 2026-07-06):
#   Group 1 -- base ring metrics + housing + household composition (20)
#   Group 2 -- income distribution B19001 (17)
#   Group 3 -- male age brackets from B01001 (17)
#   Group 4 -- female age brackets from B01001 (17)
# fetch_acs_values_for_block_groups() calls the ACS API once per (tract,
# group) pair and merges the per-BG rows across groups. So a scan pull
# for one parcel does <tracts touched> * 4 requests instead of 1, all
# in the on-demand demographics endpoint, never in the scan pipeline.

ACS_VARIABLES_BASE = {
    # Population + median income + median age + total housing units
    "B01003_001E": "total_population",
    "B01003_001M": "total_population_moe",
    "B19013_001E": "median_household_income",
    "B19013_001M": "median_household_income_moe",
    "B01002_001E": "median_age",
    "B25001_001E": "total_housing_units",
    # Housing values + rent (homebuilder / multifamily inputs)
    "B25077_001E": "median_home_value",
    "B25077_001M": "median_home_value_moe",
    "B25064_001E": "median_gross_rent",
    "B25064_001M": "median_gross_rent_moe",
    "B25071_001E": "rent_burden_pct",  # median gross rent as % of household income
    # Tenure (owner vs renter)
    "B25003_001E": "tenure_total",
    "B25003_002E": "owner_occupied",
    "B25003_003E": "renter_occupied",
    # Household size + family composition
    "B25010_001E": "avg_hh_size",
    "B25010_002E": "avg_hh_size_owner",
    "B25010_003E": "avg_hh_size_renter",
    "B11001_001E": "households_total",
    "B11001_002E": "households_family",
    "B11001_007E": "households_nonfamily",
}

# Income distribution -- 17 buckets. `_001E` is the total households
# denominator (matches B25003 minus vacant, close to B11001_001E); the
# 16 bucket variables sum to it exactly (confirmed live via Phase B
# probe against Pasco BG 1, 801 = 801).
ACS_VARIABLES_INCOME = {
    "B19001_001E": "income_bucket_total",
    "B19001_002E": "income_lt_10k",
    "B19001_003E": "income_10_15k",
    "B19001_004E": "income_15_20k",
    "B19001_005E": "income_20_25k",
    "B19001_006E": "income_25_30k",
    "B19001_007E": "income_30_35k",
    "B19001_008E": "income_35_40k",
    "B19001_009E": "income_40_45k",
    "B19001_010E": "income_45_50k",
    "B19001_011E": "income_50_60k",
    "B19001_012E": "income_60_75k",
    "B19001_013E": "income_75_100k",
    "B19001_014E": "income_100_125k",
    "B19001_015E": "income_125_150k",
    "B19001_016E": "income_150_200k",
    "B19001_017E": "income_200k_plus",
}

# Age distribution from B01001. ACS splits by sex; we pull only the age
# brackets needed for the four target bins and sum male+female at
# aggregation time. Bracket-to-code mapping per official ACS 2023
# variable dictionary:
#   003 M / 027 F : under 5           014 M / 038 F : 40 to 44
#   004 M / 028 F : 5 to 9            020 M / 044 F : 65 and 66
#   005 M / 029 F : 10 to 14          021 M / 045 F : 67 to 69
#   006 M / 030 F : 15 to 17          022 M / 046 F : 70 to 74
#   008 M / 032 F : 20                023 M / 047 F : 75 to 79
#   009 M / 033 F : 21                024 M / 048 F : 80 to 84
#   010 M / 034 F : 22 to 24          025 M / 049 F : 85 and over
#   011 M / 035 F : 25 to 29
#   012 M / 036 F : 30 to 34
#   013 M / 037 F : 35 to 39
ACS_VARIABLES_AGE_MALE = {
    "B01001_003E": "m_u5",   "B01001_004E": "m_5_9",  "B01001_005E": "m_10_14",
    "B01001_006E": "m_15_17","B01001_008E": "m_20",   "B01001_009E": "m_21",
    "B01001_010E": "m_22_24","B01001_011E": "m_25_29","B01001_012E": "m_30_34",
    "B01001_013E": "m_35_39","B01001_014E": "m_40_44","B01001_020E": "m_65_66",
    "B01001_021E": "m_67_69","B01001_022E": "m_70_74","B01001_023E": "m_75_79",
    "B01001_024E": "m_80_84","B01001_025E": "m_85_plus",
}
ACS_VARIABLES_AGE_FEMALE = {
    "B01001_027E": "f_u5",   "B01001_028E": "f_5_9",  "B01001_029E": "f_10_14",
    "B01001_030E": "f_15_17","B01001_032E": "f_20",   "B01001_033E": "f_21",
    "B01001_034E": "f_22_24","B01001_035E": "f_25_29","B01001_036E": "f_30_34",
    "B01001_037E": "f_35_39","B01001_038E": "f_40_44","B01001_044E": "f_65_66",
    "B01001_045E": "f_67_69","B01001_046E": "f_70_74","B01001_047E": "f_75_79",
    "B01001_048E": "f_80_84","B01001_049E": "f_85_plus",
}

# All 4 groups combined -- the master mapping. Kept for backward
# compatibility with any external caller reading ACS_VARIABLES.
ACS_VARIABLES = {
    **ACS_VARIABLES_BASE,
    **ACS_VARIABLES_INCOME,
    **ACS_VARIABLES_AGE_MALE,
    **ACS_VARIABLES_AGE_FEMALE,
}

_ACS_VARIABLE_GROUPS = [
    ACS_VARIABLES_BASE, ACS_VARIABLES_INCOME,
    ACS_VARIABLES_AGE_MALE, ACS_VARIABLES_AGE_FEMALE,
]

RING_RADII_MILES = (5, 10, 15)

MILES_TO_METERS = 1609.34

# The Census Bureau uses large negative sentinel values in ACS detailed
# tables to mean "not available" / "not computed" for a given geography
# (e.g. a block group too small or with too few sampled households for
# a reliable estimate) — these are NOT real values and must never be
# summed or averaged in with real ones. Confirmed set per Census ACS
# technical documentation.
_ACS_MISSING_SENTINELS = {-666666666, -999999999, -888888888, -555555555, -333333333, -222222222}


@dataclass
class RingDemographics:
    radius_miles: float
    total_population: Optional[float]
    population_moe: Optional[float]
    median_household_income: Optional[float]
    income_moe: Optional[float]
    median_age: Optional[float]
    total_housing_units: Optional[float]
    density_per_sqmi: Optional[float]
    block_groups_included: int
    block_groups_partial: int
    # Phase C (2026-07-06): homebuilder/multifamily metrics.
    # Home value / rent are pop-weighted averages of per-BG medians
    # (same caveat as median_household_income). Homeownership/renter
    # percentages are computed from summed owner/renter counts (no
    # median weirdness for shares).
    median_home_value: Optional[float] = None
    home_value_moe: Optional[float] = None
    median_gross_rent: Optional[float] = None
    gross_rent_moe: Optional[float] = None
    rent_burden_pct: Optional[float] = None  # median gross rent as % of household income
    homeownership_rate_pct: Optional[float] = None  # 0-100
    renter_occupied_pct: Optional[float] = None  # 0-100
    avg_household_size: Optional[float] = None  # pop-weighted average across BGs
    family_household_pct: Optional[float] = None  # 0-100
    # Income distribution: dict of bucket -> count summed across BGs.
    # Bucket keys use the same friendly names as ACS_VARIABLES_INCOME
    # (e.g. "income_lt_10k", "income_75_100k").
    income_distribution: Optional[dict] = None
    # Age distribution collapsed into the 4 target bins Tyler asked for:
    #   under_18, age_20_34, age_25_44, age_65_plus
    # Values are counts (sum male + female sum across BGs). Overlap
    # between age_20_34 and age_25_44 (both include 25-29 and 30-34)
    # is intentional -- 20-34 is multifamily-relevant, 25-44 is home-
    # buyer-relevant.
    age_distribution: Optional[dict] = None
    # Debug-only: per-block-group raw ACS values used to compute the
    # aggregated ring stats. Populated when compute_ring_demographics is
    # called with include_block_group_detail=True. Used by the
    # /api/parcels/{id}/demographics?debug=1 endpoint for hand-
    # verification. Never populated during normal front-end calls to
    # avoid bloating the response.
    block_group_detail: Optional[list[dict]] = None


def _require_deps():
    if Point is None:
        raise ImportError(
            "shapely is required for ring demographics. "
            "Install with: pip install shapely"
        )


def fetch_block_groups_near(lat: float, lon: float, max_radius_miles: float, census_api_key: str):
    """
    Fetch block group polygons within max_radius_miles of a point,
    via TIGERweb. Only fetches once per parcel for the largest radius
    requested (15 miles by default), then reuses the same feature set
    to compute all three rings — since block groups near the outer
    ring are a superset of those near the inner rings, one query
    services all three radii.
    """
    _require_deps()
    buffer_degrees = max_radius_miles / 69.0  # rough miles-to-degrees at this latitude; refine with a proper projection before production use
    envelope = {
        "xmin": lon - buffer_degrees, "xmax": lon + buffer_degrees,
        "ymin": lat - buffer_degrees, "ymax": lat + buffer_degrees,
        "spatialReference": {"wkid": 4326},
    }
    resp = requests.get(f"{TIGERWEB_BLOCKGROUP_URL}/query", params={
        "geometry": json.dumps(envelope), "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "STATE,COUNTY,TRACT,BLKGRP,GEOID",
        "returnGeometry": "true", "f": "json", "inSR": 4326,
        # Without this, the service returns geometry in its own default
        # SR (Web Mercator, meters) while every distance comparison in
        # compute_ring_demographics() below is done in degrees — the
        # same class of spatial-reference mismatch bug already found
        # and fixed in scan_orchestrator.py's FLUM neighbor query.
        "outSR": 4326,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"TIGERweb query error: {data['error']}")
    return data.get("features", [])


@dataclass
class PopulationTrend:
    """
    Phase D (2026-07-06): population-trend metric split into two parts
    per Tyler's B+C combined direction --
      1. County-level: reliable (FIPS codes stable across vintages).
      2. Ring-level: directional-only. Uses the same block-group GEOIDs
         from the 2023 vintage against the 2018 ACS -- BG boundaries
         were redrawn 2020, so the 2018 numbers for a "2023 BG" may
         reflect entirely different physical geography. Flagged.
    """
    baseline_year: int
    current_year: int
    # County reliable numbers
    county_name: Optional[str]  # e.g. "Pasco County, Florida" -- keeps the county visible in the payload so a wrong-county lookup is obvious at a glance
    county_state_fips: Optional[str]
    county_fips: Optional[str]
    county_population_baseline: Optional[int]
    county_population_current: Optional[int]
    county_growth_pct: Optional[float]
    # Ring directional-only numbers
    ring_population_baseline_directional: Optional[int]
    ring_population_current: Optional[int]
    ring_growth_pct_directional: Optional[float]
    ring_note: str  # human-readable caveat about BG boundary redraw


def fetch_county_population(
    state_fips: str, county_fips: str, year: int, census_api_key: str,
) -> tuple[Optional[int], Optional[str]]:
    """
    Query one county's total population + display NAME from the
    specified ACS 5-year vintage. Returns (population, name) or
    (None, None) on failure -- callers keep names visible in payloads
    so a wrong-county lookup shows up as e.g. "Hernando County"
    instead of hiding behind an anonymous +10.3% growth number.
    """
    url = (
        f"https://api.census.gov/data/{year}/acs/acs5"
        f"?get=NAME,B01003_001E&for=county:{county_fips}&in=state:{state_fips}"
        f"&key={census_api_key}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    try:
        rows = resp.json()
    except ValueError:
        raise RuntimeError(
            f"Census {year} ACS county query did not return JSON. "
            f"Raw response start: {resp.text[:200]!r}"
        )
    if len(rows) < 2:
        return None, None
    header, *data_rows = rows
    col_index = {name: i for i, name in enumerate(header)}
    val = _parse_acs_value(data_rows[0][col_index["B01003_001E"]])
    name = data_rows[0][col_index["NAME"]] if "NAME" in col_index else None
    return (int(val) if val is not None else None), name


def fetch_ring_population_directional(
    block_group_geoids: list[str],
    year: int,
    census_api_key: str,
) -> Optional[int]:
    """
    Sum B01003_001E from the specified vintage across the given BG
    GEOIDs. Directional only: 2018 ACS uses 2010 Census BG geometry,
    so the same numeric GEOIDs from 2023 (Phase B confirmed this
    silently misleading) may not represent the same physical area.
    """
    if not block_group_geoids:
        return None
    partial = _fetch_acs_group_for_block_groups(
        block_group_geoids,
        {"B01003_001E": "total_population"},
        census_api_key,
        acs5_base=f"https://api.census.gov/data/{year}/acs/acs5",
    )
    total = 0
    for values in partial.values():
        pop = values.get("total_population")
        if pop is not None:
            total += int(pop)
    return total if partial else None


def fetch_population_trend(
    state_fips: str,
    county_fips: str,
    ring_block_group_geoids: list[str],
    ring_current_population: int,
    census_api_key: str,
) -> PopulationTrend:
    """
    Compute the two-part population trend for the demographics response.
    County-level uses stable FIPS; ring-level is directional-only with
    a flag. Both fetches fail gracefully -- an unreachable Census
    endpoint yields None fields rather than blowing up the parent
    demographics response.
    """
    baseline = CENSUS_ACS5_TREND_BASELINE_YEAR
    current = CENSUS_ACS5_CURRENT_YEAR

    # County (reliable) -- pull both population + display NAME so the
    # payload identifies which county was actually looked up.
    county_name: Optional[str] = None
    try:
        county_baseline, name_baseline = fetch_county_population(
            state_fips, county_fips, baseline, census_api_key,
        )
        county_name = name_baseline
    except Exception:  # noqa: BLE001
        county_baseline = None
    try:
        county_current, name_current = fetch_county_population(
            state_fips, county_fips, current, census_api_key,
        )
        # Prefer the current-vintage NAME when both are available; both
        # should match since FIPS codes are stable.
        county_name = name_current or county_name
    except Exception:  # noqa: BLE001
        county_current = None

    county_growth_pct = None
    if county_baseline and county_current:
        county_growth_pct = (county_current - county_baseline) / county_baseline * 100.0

    # Ring (directional-only)
    try:
        ring_baseline_directional = fetch_ring_population_directional(
            ring_block_group_geoids, baseline, census_api_key,
        )
    except Exception:  # noqa: BLE001
        ring_baseline_directional = None

    ring_growth_pct_directional = None
    if ring_baseline_directional and ring_current_population:
        ring_growth_pct_directional = (
            (ring_current_population - ring_baseline_directional) / ring_baseline_directional * 100.0
        )

    ring_note = (
        f"Directional only: 2018 ACS uses 2010 Census block-group geometry; "
        f"2023 ACS uses 2020 Census geometry. The same 12-digit GEOIDs from "
        f"the current ring were queried against the 2018 vintage -- some may "
        f"resolve to different physical areas or return no data. Trust the "
        f"county-level trend as the reliable number; treat the ring-level "
        f"trend as a rough sense of neighborhood momentum, not a precise "
        f"apples-to-apples growth rate."
    )

    return PopulationTrend(
        baseline_year=baseline,
        current_year=current,
        county_name=county_name,
        county_state_fips=state_fips,
        county_fips=county_fips,
        county_population_baseline=county_baseline,
        county_population_current=county_current,
        county_growth_pct=county_growth_pct,
        ring_population_baseline_directional=ring_baseline_directional,
        ring_population_current=ring_current_population,
        ring_growth_pct_directional=ring_growth_pct_directional,
        ring_note=ring_note,
    )


def _pop_weighted_avg(pairs: list[tuple[float, float]]) -> Optional[float]:
    """
    Population-weighted average: sum(value_i * pop_i) / sum(pop_i).
    Returns None if the input list is empty or the total weight is 0.
    Used for per-block-group median aggregation across a ring.
    """
    if not pairs:
        return None
    total_weight = sum(p for _, p in pairs)
    if total_weight <= 0:
        return None
    return sum(v * p for v, p in pairs) / total_weight


def _parse_acs_value(raw) -> Optional[float]:
    """Convert one ACS API cell to a float, or None if missing/suppressed."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if int(value) in _ACS_MISSING_SENTINELS:
        return None
    return value


def _fetch_acs_group_for_block_groups(
    block_group_geoids: list[str],
    variable_group: dict[str, str],
    census_api_key: str,
    acs5_base: str = CENSUS_ACS5_BASE,
) -> dict[str, dict]:
    """
    Fetch one variable-group's values for a list of block group GEOIDs.
    See ACS_VARIABLES_BASE / _INCOME / _AGE_* for group definitions.
    Split into groups because the ACS API caps a single request at 50
    variables and the full set now hits ~70 per Phase B.
    """
    if not block_group_geoids or not variable_group:
        return {}

    variables = ",".join(variable_group.keys())

    by_tract: dict[tuple[str, str, str], list[str]] = {}
    for geoid in block_group_geoids:
        if len(geoid) != 12:
            # Malformed/unexpected GEOID shape from TIGERweb — skip this
            # one block group rather than fail the whole ring over it.
            continue
        state, county, tract, block_group = geoid[0:2], geoid[2:5], geoid[5:11], geoid[11:12]
        by_tract.setdefault((state, county, tract), []).append(block_group)

    results: dict[str, dict] = {}
    for (state, county, tract), block_groups in by_tract.items():
        # Built as a literal query string rather than via requests'
        # `params=` dict: the ACS API's `&in=` value uses `+` as a
        # literal separator between state/county/tract clauses, which
        # `params=` would percent-encode into `%2B` and break.
        url = (
            f"{acs5_base}?get=NAME,{variables}"
            f"&for=block%20group:{','.join(block_groups)}"
            f"&in=state:{state}+county:{county}+tract:{tract}"
            f"&key={census_api_key}"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        try:
            rows = resp.json()
        except ValueError:
            # Confirmed live: an invalid/expired census_api_key returns
            # HTTP 200 with an HTML "Invalid Key" error page, not JSON
            # or a 4xx — surface this clearly instead of a raw
            # JSONDecodeError.
            raise RuntimeError(
                "Census ACS API did not return JSON — likely an invalid "
                f"CENSUS_API_KEY. Raw response start: {resp.text[:200]!r}"
            )
        header, *data_rows = rows
        col_index = {name: i for i, name in enumerate(header)}

        for row in data_rows:
            row_geoid = (
                f"{row[col_index['state']]}{row[col_index['county']]}"
                f"{row[col_index['tract']]}{row[col_index['block group']]}"
            )
            entry = results.setdefault(row_geoid, {})
            entry.update({
                friendly_name: _parse_acs_value(row[col_index[var_code]])
                for var_code, friendly_name in variable_group.items()
            })

    return results


def fetch_acs_values_for_block_groups(
    block_group_geoids: list[str],
    census_api_key: str,
) -> dict[str, dict]:
    """
    Fetch every ACS variable in every group for the given block groups.
    Merges the per-group results so callers get one dict per BG with
    every friendly-name key populated.

    Makes <tracts_touched> * 4 requests (one per variable group). This
    is on-demand only, not per-scan, and the scan pipeline never calls
    ring_demographics at all.
    """
    if not block_group_geoids:
        return {}
    merged: dict[str, dict] = {}
    for group in _ACS_VARIABLE_GROUPS:
        partial = _fetch_acs_group_for_block_groups(
            block_group_geoids, group, census_api_key,
        )
        for geoid, values in partial.items():
            merged.setdefault(geoid, {}).update(values)
    return merged


def find_bg_containing_point(features: list, lat: float, lon: float) -> Optional[str]:
    """
    Return the GEOID of the block-group polygon whose geometry contains
    the given point, or -- if no polygon contains the point (rare edge
    case at BG boundaries or if the envelope query missed one) -- the
    GEOID of the BG whose centroid is closest to the point. Called
    after fetch_block_groups_near to determine the parcel's county for
    the population-trend county lookup. Fixes the "first BG in the
    ring might be in a different county" bug (2026-07-06).
    """
    _require_deps()
    p = Point(lon, lat)
    closest_geoid: Optional[str] = None
    closest_distance = float("inf")
    for feat in features:
        geom = feat.get("geometry")
        attrs = feat.get("attributes", {})
        if geom is None:
            continue
        try:
            shape = esri_json_to_shapely(geom)
        except (ValueError, TypeError, KeyError):
            continue
        if shape.contains(p):
            return attrs.get("GEOID")
        try:
            d = p.distance(shape.centroid)
            if d < closest_distance:
                closest_distance = d
                closest_geoid = attrs.get("GEOID")
        except (ValueError, AttributeError):
            continue
    return closest_geoid


def compute_ring_demographics(
    parcel_centroid_lat: float,
    parcel_centroid_lon: float,
    census_api_key: str,
    include_block_group_detail: bool = False,
) -> tuple[list[RingDemographics], Optional[str]]:
    """
    The actual on-demand entry point: given a parcel's centroid, return
    population/income/age/housing stats for 5, 10, and 15-mile rings.

    This is the ONLY function the UI should call, and only when a user
    explicitly requests it for a specific parcel — never from the scan
    orchestrator's per-candidate loop. Wire this to a button labeled
    something like "Pull area demographics" on the parcel detail view,
    not to any bulk/batch scan path.
    """
    _require_deps()
    features = fetch_block_groups_near(
        parcel_centroid_lat, parcel_centroid_lon, max(RING_RADII_MILES), census_api_key
    )
    # Fix (2026-07-06): identify the block group that actually contains
    # the parcel centroid, so the caller can use its state+county FIPS
    # for the county-level trend lookup instead of picking whichever
    # BG happens to be first in the ring's list (which could be in an
    # adjacent county if the ring straddles a county boundary -- exactly
    # the bug that made a Pasco parcel report Hernando County's trend).
    containing_bg_geoid = find_bg_containing_point(
        features, parcel_centroid_lat, parcel_centroid_lon,
    )

    results = []
    center = Point(parcel_centroid_lon, parcel_centroid_lat)

    for radius in RING_RADII_MILES:
        radius_degrees = radius / 69.0  # same rough conversion caveat as above
        included_geoids = []
        full_count = 0
        partial_count = 0

        for feat in features:
            geom = feat.get("geometry")
            if geom is None:
                continue
            try:
                bg_shape = esri_json_to_shapely(geom)
            except (ValueError, KeyError):
                continue  # skip a malformed block-group geometry rather than fail the whole ring
            distance = center.distance(bg_shape.centroid)
            if distance <= radius_degrees:
                included_geoids.append(feat["attributes"]["GEOID"])
                # Whether this block group is fully or only partially
                # within the ring (its own geometry crosses the ring
                # boundary) determines whether full-value or apportioned
                # value should be used — that distinction is not yet
                # implemented here; this MVP treats "centroid within
                # radius" as fully included, which is a simplification
                # of the weighted-centroid method described in the module
                # docstring and should be replaced with real block-level
                # apportionment before relying on the results for
                # anything other than a rough estimate.
                full_count += 1

        acs_data = fetch_acs_values_for_block_groups(included_geoids, census_api_key)
        total_pop = sum(v.get("total_population", 0) or 0 for v in acs_data.values())
        area_sqmi = 3.14159 * radius ** 2

        # Population is a count, so summing it across block groups is
        # valid. Its margin of error is NOT summed directly — Census
        # methodology combines MOEs across geographies via root-sum-of-
        # squares (assuming approximate independence between block
        # groups), not a plain sum.
        pop_moes = [v["total_population_moe"] for v in acs_data.values() if v.get("total_population_moe") is not None]
        pop_moe = math.sqrt(sum(m ** 2 for m in pop_moes)) if pop_moes else None

        # Population-weighted average of per-block-group medians (2026-
        # 07-06 Phase A). This is NOT a true median of the aggregate
        # ring's income/age distribution -- that would require the full
        # distributions per block group, not just the medians. It IS a
        # defensible approximation when the constituent block-group
        # medians are similar; when they're not, the weighted average
        # can differ meaningfully from a true median. The frontend
        # labels this as "pop.-weighted avg. of block-group medians"
        # rather than "median" for exactly this reason.
        income_pairs: list[tuple[float, float]] = []
        income_moe_pairs: list[tuple[float, float]] = []
        age_pairs: list[tuple[float, float]] = []
        # Phase C additions: home value / rent / rent burden / avg HH
        # size get the same pop-weighted-average-of-medians treatment.
        home_value_pairs: list[tuple[float, float]] = []
        home_value_moe_pairs: list[tuple[float, float]] = []
        gross_rent_pairs: list[tuple[float, float]] = []
        gross_rent_moe_pairs: list[tuple[float, float]] = []
        rent_burden_pairs: list[tuple[float, float]] = []
        avg_hh_size_pairs: list[tuple[float, float]] = []
        # Phase C count-based aggregates: tenure (owner/renter),
        # family/nonfamily, income buckets, age buckets. Counts are
        # simply summed across BGs since block groups partition the ring
        # (roughly) and we're not double-counting anyone.
        sum_tenure_total = 0.0
        sum_owner = 0.0
        sum_renter = 0.0
        sum_hh_total = 0.0
        sum_hh_family = 0.0
        income_bucket_sums: dict[str, float] = {}
        age_bin_sums = {"under_18": 0.0, "age_20_34": 0.0,
                        "age_25_44": 0.0, "age_65_plus": 0.0}
        for v in acs_data.values():
            pop = v.get("total_population") or 0
            if pop <= 0:
                continue
            if v.get("median_household_income") is not None:
                income_pairs.append((v["median_household_income"], pop))
            if v.get("median_household_income_moe") is not None:
                income_moe_pairs.append((v["median_household_income_moe"], pop))
            if v.get("median_age") is not None:
                age_pairs.append((v["median_age"], pop))
            if v.get("median_home_value") is not None:
                home_value_pairs.append((v["median_home_value"], pop))
            if v.get("median_home_value_moe") is not None:
                home_value_moe_pairs.append((v["median_home_value_moe"], pop))
            if v.get("median_gross_rent") is not None:
                gross_rent_pairs.append((v["median_gross_rent"], pop))
            if v.get("median_gross_rent_moe") is not None:
                gross_rent_moe_pairs.append((v["median_gross_rent_moe"], pop))
            if v.get("rent_burden_pct") is not None:
                rent_burden_pairs.append((v["rent_burden_pct"], pop))
            if v.get("avg_hh_size") is not None:
                avg_hh_size_pairs.append((v["avg_hh_size"], pop))
            sum_tenure_total += v.get("tenure_total") or 0
            sum_owner += v.get("owner_occupied") or 0
            sum_renter += v.get("renter_occupied") or 0
            sum_hh_total += v.get("households_total") or 0
            sum_hh_family += v.get("households_family") or 0
            for bucket_key in ACS_VARIABLES_INCOME.values():
                if bucket_key == "income_bucket_total":
                    continue
                income_bucket_sums[bucket_key] = (
                    income_bucket_sums.get(bucket_key, 0.0)
                    + (v.get(bucket_key) or 0)
                )
            # Sum male + female age bracket counts into the 4 target bins.
            def _sum_bracket(*keys) -> float:
                return sum((v.get(k) or 0) for k in keys)
            age_bin_sums["under_18"] += _sum_bracket(
                "m_u5", "m_5_9", "m_10_14", "m_15_17",
                "f_u5", "f_5_9", "f_10_14", "f_15_17",
            )
            age_bin_sums["age_20_34"] += _sum_bracket(
                "m_20", "m_21", "m_22_24", "m_25_29", "m_30_34",
                "f_20", "f_21", "f_22_24", "f_25_29", "f_30_34",
            )
            age_bin_sums["age_25_44"] += _sum_bracket(
                "m_25_29", "m_30_34", "m_35_39", "m_40_44",
                "f_25_29", "f_30_34", "f_35_39", "f_40_44",
            )
            age_bin_sums["age_65_plus"] += _sum_bracket(
                "m_65_66", "m_67_69", "m_70_74", "m_75_79", "m_80_84", "m_85_plus",
                "f_65_66", "f_67_69", "f_70_74", "f_75_79", "f_80_84", "f_85_plus",
            )

        income_est = _pop_weighted_avg(income_pairs)
        age_est = _pop_weighted_avg(age_pairs)
        # Income MOE across block groups: population-weighted average
        # of per-BG MOEs. NOT strictly correct (true propagation for a
        # weighted median has no closed form given only per-BG medians),
        # but gives the caller a rough sense of the estimate's
        # uncertainty rather than nothing.
        income_moe_est = _pop_weighted_avg(income_moe_pairs)
        home_value_est = _pop_weighted_avg(home_value_pairs)
        home_value_moe_est = _pop_weighted_avg(home_value_moe_pairs)
        gross_rent_est = _pop_weighted_avg(gross_rent_pairs)
        gross_rent_moe_est = _pop_weighted_avg(gross_rent_moe_pairs)
        rent_burden_est = _pop_weighted_avg(rent_burden_pairs)
        avg_hh_size_est = _pop_weighted_avg(avg_hh_size_pairs)

        homeownership_pct = (
            (sum_owner / sum_tenure_total * 100.0)
            if sum_tenure_total > 0 else None
        )
        renter_pct = (
            (sum_renter / sum_tenure_total * 100.0)
            if sum_tenure_total > 0 else None
        )
        family_hh_pct = (
            (sum_hh_family / sum_hh_total * 100.0)
            if sum_hh_total > 0 else None
        )

        block_group_detail = None
        if include_block_group_detail:
            block_group_detail = [
                {
                    "geoid": geoid,
                    "total_population": v.get("total_population"),
                    "total_population_moe": v.get("total_population_moe"),
                    "median_household_income": v.get("median_household_income"),
                    "median_household_income_moe": v.get("median_household_income_moe"),
                    "median_age": v.get("median_age"),
                    "total_housing_units": v.get("total_housing_units"),
                }
                for geoid, v in acs_data.items()
            ]

        results.append(RingDemographics(
            radius_miles=radius,
            total_population=total_pop if acs_data else None,
            population_moe=pop_moe,
            median_household_income=income_est,
            income_moe=income_moe_est,
            median_age=age_est,
            total_housing_units=sum(v.get("total_housing_units", 0) or 0 for v in acs_data.values()) if acs_data else None,
            density_per_sqmi=(total_pop / area_sqmi) if acs_data and area_sqmi else None,
            block_groups_included=full_count,
            block_groups_partial=partial_count,
            median_home_value=home_value_est,
            home_value_moe=home_value_moe_est,
            median_gross_rent=gross_rent_est,
            gross_rent_moe=gross_rent_moe_est,
            rent_burden_pct=rent_burden_est,
            homeownership_rate_pct=homeownership_pct,
            renter_occupied_pct=renter_pct,
            avg_household_size=avg_hh_size_est,
            family_household_pct=family_hh_pct,
            income_distribution=income_bucket_sums if income_bucket_sums else None,
            age_distribution=age_bin_sums if any(age_bin_sums.values()) else None,
            block_group_detail=block_group_detail,
        ))

    return results, containing_bg_geoid
