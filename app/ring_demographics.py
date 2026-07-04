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

# ACS variables pulled for each ring. Population and housing units are
# base counts; income and age come with their own margin-of-error
# companions (the M-suffixed variables), which should be surfaced
# alongside the estimate in the UI rather than dropped, since ACS
# 5-year block-group estimates can carry wide margins in low-population
# areas — exactly the rural areas this tool spends most of its time in.
ACS_VARIABLES = {
    "B01003_001E": "total_population",
    "B01003_001M": "total_population_moe",
    "B19013_001E": "median_household_income",
    "B19013_001M": "median_household_income_moe",
    "B01002_001E": "median_age",
    "B25001_001E": "total_housing_units",
}

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


def fetch_acs_values_for_block_groups(block_group_geoids: list[str], census_api_key: str) -> dict[str, dict]:
    """
    Batch-fetch ACS 5-year values for a list of block group GEOIDs.

    The ACS API's `&for=block group:` geography predicate only accepts
    multiple block group codes within a SINGLE tract per call — it has
    no way to span tracts in one request. So GEOIDs (12-digit:
    state[2]+county[3]+tract[6]+block group[1]) are grouped by their
    (state, county, tract) prefix, and one request is issued per group,
    each requesting every block group in that tract at once (there's no
    documented cap on block groups per call within one tract, and a
    single tract only ever has a handful of block groups, so no further
    chunking is needed there).
    """
    if not block_group_geoids:
        return {}

    variables = ",".join(ACS_VARIABLES.keys())

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
            f"{CENSUS_ACS5_BASE}?get=NAME,{variables}"
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
            # JSONDecodeError, since a bad key is the single most likely
            # real-world failure here.
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
            results[row_geoid] = {
                friendly_name: _parse_acs_value(row[col_index[var_code]])
                for var_code, friendly_name in ACS_VARIABLES.items()
            }

    return results


def compute_ring_demographics(
    parcel_centroid_lat: float,
    parcel_centroid_lon: float,
    census_api_key: str,
) -> list[RingDemographics]:
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

        results.append(RingDemographics(
            radius_miles=radius,
            total_population=total_pop if acs_data else None,
            population_moe=pop_moe,
            median_household_income=None,  # medians cannot be simply averaged across block groups; needs a population-weighted approach or reporting a range
            income_moe=None,
            median_age=None,  # same caveat as median income
            total_housing_units=sum(v.get("total_housing_units", 0) or 0 for v in acs_data.values()) if acs_data else None,
            density_per_sqmi=(total_pop / area_sqmi) if acs_data and area_sqmi else None,
            block_groups_included=full_count,
            block_groups_partial=partial_count,
        ))

    return results
