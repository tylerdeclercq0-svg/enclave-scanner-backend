"""
ZIP Code Tabulation Area (ZCTA) wrapper -- Census TIGERweb.

Sourced live from `TIGERweb/tigerWMS_Current/MapServer` (2020 vintage,
confirmed live 2026-07-06):
  Layer 82 -- Counties
  Layer 2  -- 2020 Census ZIP Code Tabulation Areas

Why ZCTAs and not USPS ZIP codes: actual USPS ZIP codes are delivery
routes, not geographic areas; they have no clean polygon representation.
ZCTAs are the Census Bureau's polygon approximation of USPS ZIPs and are
what every "ZIP code polygon" GIS dataset actually is under the hood.

Coverage math verified live for St. Johns (STATE='12', COUNTY='109') on
2026-07-06: 22 ZCTAs intersect the county boundary; total ag-candidate
parcels in St. Johns countywide is 4,176 with the tool's standard ag
WHERE clause (no acreage filter); sample density ZCTA 32033 contains
577 ag-candidate parcels within just that one ZIP section.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Optional

import requests

from arcgis_client import query_layer
from parcel_fetcher import AREA_SR


_TIGERWEB_BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"
COUNTIES_LAYER_URL = f"{_TIGERWEB_BASE}/82"
ZCTA_LAYER_URL = f"{_TIGERWEB_BASE}/2"

# Every Florida county in this project's registry is state 12; if this
# ever expands beyond FL, add a state code to CountyEndpoint and read
# from there.
_FLORIDA_STATE_FIPS = "12"

# Confirmed as the same code family already stored in
# CountyEndpoint.fips (DOR county number). BUT DOR county numbers are
# NOT the same as US Census county FIPS -- e.g. Pasco DOR=61, Census
# FIPS=101; St. Johns DOR=65, Census FIPS=109. Explicit mapping to
# avoid confusion. Verified against the Census reference at
# https://www.census.gov/library/reference/code-lists/ansi.html.
CENSUS_COUNTY_FIPS = {
    "pasco":    "101",
    "nassau":   "089",
    "st_johns": "109",
    "osceola":  "097",
    # These aren't scan-live yet, but recorded for future coverage:
    "hillsborough": "057",
    "orange":       "095",
    "sarasota":     "115",
    "manatee":      "081",
    "brevard":      "009",
    "volusia":      "127",
}


def _fetch_county_boundary(county_id: str) -> Optional[dict]:
    """Live query the Census TIGERweb Counties layer for one county's geometry (WKID 4326)."""
    census_fips = CENSUS_COUNTY_FIPS.get(county_id)
    if census_fips is None:
        return None
    resp = requests.post(f"{COUNTIES_LAYER_URL}/query", data={
        "where": f"STATE='{_FLORIDA_STATE_FIPS}' AND COUNTY='{census_fips}'",
        "outFields": "NAME,STATE,COUNTY,GEOID",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }, timeout=60)
    resp.raise_for_status()
    feats = resp.json().get("features", [])
    if not feats:
        return None
    return feats[0].get("geometry")


@lru_cache(maxsize=32)
def get_county_zctas(county_id: str) -> tuple[dict, ...]:
    """
    Return every ZCTA polygon that intersects a given county boundary.
    Cached per-process because these polygons don't change between vintage
    updates (roughly decennial). Each entry has `zcta5`, `geoid`,
    `centroid_lat`, `centroid_lon`, `areland_sqm`, and the ZCTA's
    geometry in AREA_SR (WKID 3086) for later spatial operations.
    """
    boundary = _fetch_county_boundary(county_id)
    if boundary is None:
        raise RuntimeError(
            f"Could not find Census TIGERweb county boundary for "
            f"'{county_id}'. Add its Census FIPS to CENSUS_COUNTY_FIPS."
        )

    resp = requests.post(f"{ZCTA_LAYER_URL}/query", data={
        "geometry": json.dumps(boundary),
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": "ZCTA5,GEOID,BASENAME,CENTLAT,CENTLON,AREALAND",
        "returnGeometry": "true",
        "outSR": str(AREA_SR),
        "f": "json",
    }, timeout=90)
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"ZCTA intersect query error: {payload['error']}")

    zctas: list[dict] = []
    for feat in payload.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry")
        zctas.append({
            "zcta5": attrs.get("ZCTA5") or attrs.get("BASENAME"),
            "geoid": attrs.get("GEOID"),
            "centroid_lat": float(attrs["CENTLAT"]) if attrs.get("CENTLAT") else None,
            "centroid_lon": float(attrs["CENTLON"]) if attrs.get("CENTLON") else None,
            "arealand_sqm": float(attrs["AREALAND"]) if attrs.get("AREALAND") else None,
            "geometry": geom,
        })
    # Deterministic order: ZCTA5 ascending. Callers rely on this for
    # "the next incomplete ZCTA" progression -- documented behavior, not
    # incidental.
    zctas.sort(key=lambda z: z["zcta5"] or "")
    return tuple(zctas)


def assign_parcel_centroid_to_zcta(parcel_geometry: dict, zctas: tuple[dict, ...]) -> Optional[str]:
    """
    Return the single ZCTA5 code whose polygon contains this parcel's
    centroid, or None if the centroid falls outside every ZCTA. Uses
    centroid (not intersection) deliberately so a parcel straddling a
    ZCTA boundary is assigned to exactly one ZIP section, avoiding
    double-count across the ledger.
    """
    from encirclement import esri_json_to_shapely
    from shapely.geometry import Point, shape

    try:
        parcel_poly = esri_json_to_shapely(parcel_geometry)
        centroid: Point = parcel_poly.centroid
    except (ValueError, TypeError, AttributeError):
        return None

    for z in zctas:
        try:
            zpoly = esri_json_to_shapely(z["geometry"])
            if zpoly.contains(centroid):
                return z["zcta5"]
        except (ValueError, TypeError):
            continue
    return None


def count_parcels_in_zcta(
    county_parcel_service_url: str,
    where_clause: str,
    zcta_geometry: dict,
) -> int:
    """
    Return the count of parcels in a specific county parcel layer that
    (a) match a WHERE clause (typically the ag-use filter) AND
    (b) intersect a specific ZCTA polygon.
    Used to establish the "total_candidates" figure per ZCTA in the
    coverage ledger so completion can be measured accurately.
    """
    resp = requests.post(f"{county_parcel_service_url}/query", data={
        "where": where_clause,
        "geometry": json.dumps(zcta_geometry),
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": str(AREA_SR),
        "returnCountOnly": "true",
        "f": "json",
    }, timeout=90)
    resp.raise_for_status()
    return int(resp.json().get("count", 0))
