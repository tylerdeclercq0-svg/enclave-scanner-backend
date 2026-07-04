"""
FLU/FLUM value normalization for the wizard UI's "FLUM character" and
"surrounding density" filters/columns.

Every county spells out its own FLU category strings differently (see
county_registry.py's per-county `agricultural_flu_values`/notes — e.g.
Pasco's "AG/R", Osceola's "rural/agricultural" lowercase, St. Johns'
"CITY OF ST. AUGUSTINE" as its own category). Rather than hand-maintain
a full per-county lookup table for every possible FLU string (fragile,
and this project has been burned before by assuming a category name
means what it sounds like — see Osceola's DORCode/St. Johns' '9900'
traps documented in county_registry.py), this uses a small keyword
classifier that works across counties on the FLU string itself. This is
explicitly a best-effort bucket, not an authoritative land-use
classification — surfaced in the UI as an estimate, same caveat as the
existing attractiveness score.
"""

from __future__ import annotations

from typing import Literal, Optional

DensityBucket = Literal["rural", "suburban", "urban", "unknown"]

# Checked in order — first match wins. Order matters: e.g. "rural
# enclave" should classify as rural even though "enclave" isn't a
# density keyword, so rural keywords are checked before urban ones to
# avoid a compound category tripping the wrong bucket.
_RURAL_KEYWORDS = (
    "RURAL", "AGRICULT", "CONSERVATION", "TIMBERLAND", "SYLV",
    "NATURAL", "ACREAGE", "FOREST",
)
_SUBURBAN_KEYWORDS = (
    "SUBURBAN", "MEDIUM DENSITY", "MEDIUM-DENSITY", "MIXED USE",
    "MIXED-USE", "TRANSITIONAL",
)
_URBAN_KEYWORDS = (
    "URBAN", "HIGH DENSITY", "HIGH-DENSITY", "COMMERCIAL", "INDUSTRIAL",
    "TOWN CENTER", "CITY OF", "DOWNTOWN", "CENTRAL BUSINESS",
)


def classify_density(flu_value: Optional[str]) -> DensityBucket:
    if not flu_value:
        return "unknown"
    upper = flu_value.upper()
    for kw in _RURAL_KEYWORDS:
        if kw in upper:
            return "rural"
    for kw in _SUBURBAN_KEYWORDS:
        if kw in upper:
            return "suburban"
    for kw in _URBAN_KEYWORDS:
        if kw in upper:
            return "urban"
    return "unknown"


def dominant_segment_flu(segments) -> Optional[str]:
    """
    The FLU value with the most total shared perimeter length across a
    candidate's encirclement segments (encirclement.PerimeterSegment) —
    i.e. "what's mostly around this parcel," used for the surrounding
    density bucket. Distinct from the parcel's OWN current FLU
    designation (see determine_own_flu below) — a parcel zoned
    Agricultural can still be mostly surrounded by Urban development.
    """
    totals: dict[str, float] = {}
    for seg in segments:
        if not seg.flu_value:
            continue
        totals[seg.flu_value] = totals.get(seg.flu_value, 0.0) + seg.length
    if not totals:
        return None
    return max(totals, key=totals.get)


def determine_own_flu(
    candidate_geometry: dict,
    neighbor_features: list[dict],
    flu_field: str,
) -> Optional[str]:
    """
    The candidate parcel's OWN current FLU designation — the FLU
    category of whichever neighbor polygon covers the most of the
    candidate's own area, not just touches its boundary. Reuses
    `neighbor_features` already fetched for the encirclement check (that
    spatial query always includes the polygon the candidate itself sits
    on, since it's a buffered intersection query centered on the
    candidate) — no new ArcGIS call needed.
    """
    from encirclement import esri_json_to_shapely  # local import, same ImportError-isolation pattern as scan_orchestrator

    candidate_poly = esri_json_to_shapely(candidate_geometry)

    best_flu: Optional[str] = None
    best_area = 0.0
    for feat in neighbor_features:
        geom = feat.get("geometry")
        if geom is None:
            continue
        try:
            neighbor_poly = esri_json_to_shapely(geom)
        except (ValueError, TypeError):
            continue
        overlap = candidate_poly.intersection(neighbor_poly).area
        if overlap > best_area:
            best_area = overlap
            best_flu = feat.get("attributes", {}).get(flu_field)
    return best_flu
