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
census blocks (finer than block groups) fall inside the ring. This
sandbox has no network access to actually run this against live data —
treat this as the implementation to deploy and test in a real
environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    from shapely.geometry import shape, Point
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover
    shape = Point = unary_union = None  # type: ignore

import requests


CENSUS_ACS5_BASE = "https://api.census.gov/data/2023/acs/acs5"
TIGERWEB_BLOCKGROUP_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/State_County/MapServer/1"  # block group layer; confirm exact layer index against live service before deploying
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
    if shape is None:
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
        "geometry": str(envelope), "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "STATE,COUNTY,TRACT,BLKGRP,GEOID",
        "returnGeometry": "true", "f": "json",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("features", [])


def fetch_acs_values_for_block_groups(block_group_geoids: list[str], census_api_key: str) -> dict[str, dict]:
    """
    Batch-fetch ACS 5-year values for a list of block group GEOIDs.
    Census API caps at 50 variables per call (well under our ~6) but
    doesn't have a documented per-request GEOID limit as generous as
    one might hope in practice — batch in chunks of ~500 and merge if
    a single county's ring pulls in an unusually large number of
    block groups (very possible at 15 miles in a dense metro area).
    """
    if not block_group_geoids:
        return {}
    variables = ",".join(ACS_VARIABLES.keys())
    # GEOIDs need to be decomposed back into state+county+tract+blockgroup
    # predicates for the ACS API's &for=/&in= geography syntax — this is
    # a real implementation detail deferred here; the actual call shape is:
    # api.census.gov/data/2023/acs/acs5?get=NAME,{variables}&for=block%20group:{bg}&in=state:{st}+county:{co}+tract:{tract}&key={key}
    # batched per unique (state, county, tract) combination present in
    # block_group_geoids, since &for=block group: only accepts multiple
    # block groups within a single tract per call.
    raise NotImplementedError(
        "Batch ACS fetch requires grouping GEOIDs by state/county/tract "
        "and issuing one call per tract group — implement against real "
        "GEOIDs returned by fetch_block_groups_near() before deploying."
    )


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
            bg_shape = shape(geom)  # requires geometry already in esri-json-compatible dict form; adapt esri_json_to_shapely from encirclement.py if reusing that converter
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

        results.append(RingDemographics(
            radius_miles=radius,
            total_population=total_pop if acs_data else None,
            population_moe=None,  # MOE aggregation across block groups requires root-sum-of-squares combination per Census methodology, not implemented here
            median_household_income=None,  # medians cannot be simply averaged across block groups; needs a population-weighted approach or reporting a range
            income_moe=None,
            median_age=None,  # same caveat as median income
            total_housing_units=sum(v.get("total_housing_units", 0) or 0 for v in acs_data.values()) if acs_data else None,
            density_per_sqmi=(total_pop / area_sqmi) if acs_data and area_sqmi else None,
            block_groups_included=full_count,
            block_groups_partial=partial_count,
        ))

    return results
