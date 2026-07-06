"""
Metro-proximity signal (roadmap item 5, 2026-07-06).

For each parcel, find the nearest Florida Census "place" (incorporated
city or CDP) and compute a transparent "metro pull" score from that
place's population and median household income, discounted by distance.

Design principles baked into this module:

1. **Nearest place is computed by real distance from the parcel's
   actual centroid**, not "whichever place came back first from a
   spatial API query." That's the exact class of bug already caught +
   fixed once last session in ring_demographics.find_bg_containing_point
   (the county-attribution bug where the trend was for Hernando County
   because the wrong BG was picked from `largest_geoids[0]`). Haversine
   over every FL place is cheap (~800 places, one dot product per
   parcel), so there's no reason to shortcut.

2. **Every input to the score is stored alongside the score**, so any
   ranking is auditable directly against the formula -- no black box.

3. **The metro-pull score is NEVER blended into the tier or statutory
   pathway score.** It's a secondary sort key only. If a parcel has
   pathway score 0 (no statutory pathway matched), a huge metro-pull
   number doesn't rescue it -- statutory eligibility comes first.

4. Data sources are the same ones already used elsewhere in this
   project: TIGERweb for geometry/centroids (same MapServer used by
   ring_demographics), ACS 5-year 2023 for population + median income
   (same vintage the rest of the demographics pull uses).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional

import requests

# TIGERweb 2023 ACS vintage. Layer 28 is "Incorporated Places" and 30
# is "Census Designated Places" -- combining both matches what ACS's
# `for=place:*` returns. Confirmed via live MapServer?f=json 2026-07-06.
_TIGERWEB_BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_ACS2023/MapServer"
)
_TIGERWEB_PLACE_LAYERS = [28, 30]

# ACS 2023 5-year vintage matches ring_demographics.
_ACS_BASE = "https://api.census.gov/data/2023/acs/acs5"

FL_STATE_FIPS = "12"


@dataclass
class FLPlace:
    """One Census place (incorporated city or CDP) in Florida."""
    place_fips: str          # 5-digit code within state (e.g. "45000" = Tampa)
    name: str                # e.g. "Tampa city, Florida"
    basename: str            # e.g. "Tampa" (no type suffix, better for UI)
    centroid_lat: float      # from TIGERweb INTPTLAT (guaranteed inside polygon)
    centroid_lon: float
    population: Optional[int]
    median_household_income: Optional[float]


@dataclass
class MetroProximity:
    """
    Metro-proximity facts + score for one parcel. All raw inputs to
    the score are stored so the ranking is auditable.
    """
    place_name: str
    place_basename: str
    place_fips: str
    distance_miles: float
    place_population: Optional[int]
    place_median_hh_income: Optional[float]
    metro_pull_score: Optional[float]


_places_cache: Optional[list[FLPlace]] = None
_places_lock = threading.Lock()


def fetch_fl_places(census_api_key: str) -> list[FLPlace]:
    """
    Load FL Census places once per process. Combines:
      - TIGERweb layers 28 (Incorporated Places) + 30 (CDPs) for
        name + INTPTLAT/INTPTLON centroids
      - ACS 2023 5-year `for=place:*&in=state:12` for
        B01003_001E (population) + B19013_001E (median HH income)

    Cached at module scope guarded by a threading lock so concurrent
    scan workers don't duplicate the fetch. Roughly 800 FL places, so
    the combined payload is small and this is a one-time cost per
    process.
    """
    global _places_cache
    if _places_cache is not None:
        return _places_cache
    with _places_lock:
        if _places_cache is not None:
            return _places_cache
        _places_cache = _fetch_fl_places_uncached(census_api_key)
        return _places_cache


def _fetch_fl_places_uncached(census_api_key: str) -> list[FLPlace]:
    # 1) TIGERweb: names + centroids for both incorporated places and CDPs.
    place_meta: dict[str, tuple[str, str, float, float]] = {}
    for layer_id in _TIGERWEB_PLACE_LAYERS:
        resp = requests.get(f"{_TIGERWEB_BASE}/{layer_id}/query", params={
            "where": f"STATE='{FL_STATE_FIPS}'",
            "outFields": "GEOID,NAME,BASENAME,INTPTLAT,INTPTLON",
            "returnGeometry": "false",
            "f": "json",
        }, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                f"TIGERweb layer {layer_id} query error: {data['error']}"
            )
        for feat in data.get("features", []):
            attrs = feat["attributes"]
            geoid = attrs.get("GEOID") or ""
            # GEOID is state (2) + place (5) = 7 chars for FL places.
            if len(geoid) != 7 or not geoid.startswith(FL_STATE_FIPS):
                continue
            place_fips = geoid[2:]
            try:
                lat = float(attrs["INTPTLAT"])
                lon = float(attrs["INTPTLON"])
            except (KeyError, TypeError, ValueError):
                continue
            name = attrs.get("NAME") or attrs.get("BASENAME") or "unknown"
            basename = attrs.get("BASENAME") or name
            place_meta[place_fips] = (name, basename, lat, lon)

    # 2) ACS: population + median HH income for every FL place, one query.
    url = (
        f"{_ACS_BASE}?get=NAME,B01003_001E,B19013_001E"
        f"&for=place:*&in=state:{FL_STATE_FIPS}"
        f"&key={census_api_key}"
    )
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    try:
        rows = resp.json()
    except ValueError:
        raise RuntimeError(
            f"Census place-level query did not return JSON; likely bad "
            f"CENSUS_API_KEY. Raw response start: {resp.text[:200]!r}"
        )
    if not rows or len(rows) < 2:
        raise RuntimeError(
            f"Census place-level query returned no rows: {rows!r}"
        )
    header, *data_rows = rows
    col = {n: i for i, n in enumerate(header)}
    acs_data: dict[str, tuple[Optional[int], Optional[int]]] = {}
    for r in data_rows:
        place_fips = r[col["place"]]
        pop = _parse_acs_int(r[col["B01003_001E"]])
        inc = _parse_acs_int(r[col["B19013_001E"]])
        acs_data[place_fips] = (pop, inc)

    # 3) Merge. A place present in only one source is skipped -- rare,
    # since ACS and TIGERweb are both keyed to the same Census place
    # inventory for a given vintage.
    combined = []
    for place_fips, (name, basename, lat, lon) in place_meta.items():
        if place_fips not in acs_data:
            continue
        pop, inc = acs_data[place_fips]
        combined.append(FLPlace(
            place_fips=place_fips,
            name=name,
            basename=basename,
            centroid_lat=lat,
            centroid_lon=lon,
            population=pop,
            median_household_income=inc,
        ))
    return combined


def _parse_acs_int(raw) -> Optional[int]:
    """
    ACS uses sentinel negatives (e.g. -666666666) for null / margin-
    below-threshold cells. Empty strings and dashes also appear in the
    wild. Anything non-numeric or negative maps to None so downstream
    scoring can render "unknown" instead of a fabricated value.
    """
    if raw in (None, "", "-", "null"):
        return None
    try:
        n = int(float(raw))
    except (ValueError, TypeError):
        return None
    return n if n >= 0 else None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Great-circle distance in miles. Standard haversine formula --
    accurate to ~0.5% at the scale of Florida (~500 mi state span),
    plenty of resolution for a metro-proximity signal.
    """
    R = 3958.7613  # Earth radius, miles
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def nearest_places(
    parcel_lat: float,
    parcel_lon: float,
    places: list[FLPlace],
    max_miles: float = 50.0,
    max_results: int = 5,
) -> list[tuple[FLPlace, float]]:
    """
    Real distance calc from the parcel's actual centroid to every FL
    place; sort ascending; keep those within max_miles; return top-N.

    Deliberately NOT "whichever place a spatial API returned first" --
    that's the same class of bug already caught last session
    (find_bg_containing_point fix, commit 44277a5). At ~800 FL places
    this is a cheap linear scan.
    """
    ranked = []
    for p in places:
        d = haversine_miles(parcel_lat, parcel_lon, p.centroid_lat, p.centroid_lon)
        if d <= max_miles:
            ranked.append((p, d))
    ranked.sort(key=lambda pd: pd[1])
    return ranked[:max_results]


def compute_metro_pull(
    population: Optional[int],
    median_hh_income: Optional[float],
    distance_miles: Optional[float],
) -> Optional[float]:
    """
    Transparent "metro pull" score.

    Formula (2026-07-06):
        raw   = (population * median_hh_income) / (distance_miles + 1)
        score = log10(raw)

    In plain terms:
      - population * income     = a rough proxy for the metro's total
                                  household spending power (bigger,
                                  wealthier metros pull more demand).
      - (distance_miles + 1)    = inverse-linear distance discount. The
                                  +1 avoids div-by-zero when a parcel
                                  is literally inside the metro (dist
                                  ~ 0), and keeps in-town parcels from
                                  scoring at infinity.
      - log10                   = compresses the range so real FL
                                  places fall in a readable ~5-10 band
                                  instead of hundreds-of-millions.

    Ranking direction: HIGHER score = closer to bigger, wealthier metro.

    This score is NEVER blended into the tier or statutory pathway
    score. It's a secondary sort key only -- among "confirmed
    qualifying" parcels, higher metro_pull_score sorts closer to the
    top. Every input to the score (population, income, distance) is
    stored alongside the score so a user can audit any ranking
    directly against the formula.

    Returns None whenever any input is missing so callers can render
    "unknown" rather than a fabricated zero.
    """
    if population is None or median_hh_income is None or distance_miles is None:
        return None
    if population <= 0 or median_hh_income <= 0:
        return None
    raw = (population * median_hh_income) / (distance_miles + 1.0)
    return round(math.log10(raw), 3)


def metro_proximity_for_parcel(
    parcel_lat: float,
    parcel_lon: float,
    places: list[FLPlace],
    max_miles: float = 50.0,
) -> Optional[MetroProximity]:
    """
    Convenience: nearest FL place within max_miles + its metro-pull
    score. Returns None if no place is within max_miles (very rural
    parcels; unusual in Florida but structurally possible).
    """
    ranked = nearest_places(parcel_lat, parcel_lon, places, max_miles=max_miles, max_results=1)
    if not ranked:
        return None
    place, distance = ranked[0]
    score = compute_metro_pull(place.population, place.median_household_income, distance)
    return MetroProximity(
        place_name=place.name,
        place_basename=place.basename,
        place_fips=place.place_fips,
        distance_miles=round(distance, 2),
        place_population=place.population,
        place_median_hh_income=place.median_household_income,
        metro_pull_score=score,
    )
