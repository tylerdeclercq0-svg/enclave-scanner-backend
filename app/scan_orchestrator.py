"""
Scan orchestrator — the pipeline a real backend would run when the UI's
"Run scan" button is clicked.

Order of operations, matching the actual statutory test:
  1. Pull candidate parcels from the county's OWN parcel layer
     (parcel_fetcher) filtered by acreage and that county's confirmed
     agricultural use code(s).
  2. For each candidate, run the encirclement test against the county's
     FLUM layer (encirclement) to estimate which of the five pathways
     might apply.
  3. Check statutory exclusion zones (exclusions) — Wekiva Study Area,
     Everglades Protection Area, Areas of Critical State Concern,
     conservation easements, military buffers.
  4. Score each surviving candidate for development attractiveness
     (scoring) — a business judgment layer on top of legal eligibility,
     not a part of SB 686 itself.

REWRITTEN 2026-07-03 alongside parcel_fetcher.py: step 1 now queries
each county's own parcel layer instead of the statewide cadastral layer
filtered by CO_NO (confirmed broken — see county_registry.py's
"GROUND-TRUTHED" note). ScanResultRow's fields changed to match
parcel_fetcher.CandidateParcel's new shape (use_code instead of
dor_use_code; jurisdiction added where a county's parcel layer carries
one; sale_year/sale_price dropped — not confirmed as present/consistent
across all four target counties' parcel layers this pass, unlike the
old statewide-layer fields SALE_YR1/SALE_PRC1 which were never actually
tested against live data).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from county_registry import COUNTIES
from parcel_fetcher import fetch_candidate_parcels, CandidateParcel
from encirclement import compute_encirclement, determine_pathways, EncirclementResult, get_centroid_lat_lon
from arcgis_client import query_layer
import exclusions
import scoring


@dataclass
class ScanResultRow:
    parcel_id: Optional[str]
    county_id: str
    acreage: Optional[float]
    acreage_source: str
    owner_name: Optional[str]
    owner_name_2: Optional[str]
    use_code: Optional[str]
    jurisdiction: Optional[str]
    pct_perimeter_qualifying: Optional[float]
    likely_pathways: list[int]
    exclusion_flags: list[str]
    attractiveness_score: Optional[int]
    score_breakdown: Optional[dict]
    needs_manual_review: list[str]
    centroid_lat: Optional[float] = None
    centroid_lon: Optional[float] = None


def run_county_scan(
    county_id: str,
    min_acreage: float = 20.0,
    max_acreage: float = 1280.0,
    fetch_neighbor_buffer_feet: float = 50.0,
    max_candidates: int = 25,
) -> list[ScanResultRow]:
    """
    Run the full pipeline for one county and return scored, ranked
    candidate rows ready for the UI table.

    max_candidates is intentionally small (25) as a first-deploy default.
    Each candidate parcel triggers a SEPARATE live ArcGIS query against
    the county's FLUM layer for the encirclement test, on top of the
    initial cadastral fetch — so scan time scales roughly linearly with
    candidate count. 25 candidates was chosen to keep a first real-world
    scan comfortably under common free-tier hosting request timeouts
    (Render's free tier, for instance). Raise this once real response
    times have been measured, and consider moving to a background-job
    pattern (kick off the scan, poll for results) rather than one long
    synchronous request if you want to scan hundreds of parcels at once.

    fetch_neighbor_buffer_feet controls how far past each candidate's
    boundary to query the FLUM layer for neighboring polygons — needs to
    be large enough to catch true neighbors despite minor digitization
    gaps between independently-maintained parcel and FLUM layers, but
    not so large that it pulls in parcels two properties away. 50 feet
    is a starting point, not a tuned value.
    """
    county = COUNTIES.get(county_id)
    if county is None:
        raise ValueError(f"Unknown county id: {county_id}")

    candidates = fetch_candidate_parcels(
        county_id=county_id,
        min_acreage=min_acreage,
        max_acreage=max_acreage,
        max_candidates=max_candidates,
    )

    rows: list[ScanResultRow] = []

    for parcel in candidates:
        needs_review: list[str] = []
        pathways: list[int] = []
        pct_qualifying: Optional[float] = None

        if parcel.geometry is None:
            needs_review.append(
                "No geometry returned for this parcel — encirclement "
                "test could not run. Verify manually in the county GIS viewer."
            )
        else:
            try:
                buffered_geom = _buffer_esri_geometry(
                    parcel.geometry, fetch_neighbor_buffer_feet
                )
                neighbor_features = list(query_layer(
                    county.flum_service_url,
                    return_geometry=True,
                    geometry=buffered_geom,
                    geometry_type="esriGeometryPolygon",
                    spatial_rel="esriSpatialRelIntersects",
                ))
                encirclement = compute_encirclement(
                    parcel.geometry,
                    neighbor_features,
                    flu_field=county.flu_field,
                    agricultural_flu_values=county.agricultural_flu_values,
                )
                pct_qualifying = encirclement.pct_qualifying
                pathways = determine_pathways(
                    encirclement,
                    acreage=parcel.acreage or 0,
                    adjacent_to_interstate=False,  # requires FDOT roads layer — not yet wired in
                    adjacent_to_usb=False,  # requires county-specific USB layer — only confirmed for Hillsborough
                )
                if not pathways:
                    needs_review.append(
                        "No pathway matched automatically. This often means "
                        "the parcel relies on an interstate/USB combination "
                        "(pathway 3 or 4) or rural study area (pathway 5) — "
                        "neither is fully automated yet. Don't treat this as "
                        "a definitive disqualification."
                    )
            except ImportError:
                needs_review.append(
                    "Shapely not installed in this environment — "
                    "encirclement test was skipped entirely."
                )
            except Exception as exc:  # noqa: BLE001 — surface any geometry/query failure to the reviewer rather than silently dropping the parcel
                needs_review.append(f"Encirclement test failed to run: {exc}")

        exclusion_flags = exclusions.check_exclusions(parcel)
        if exclusion_flags:
            needs_review.append(
                "Possible statutory exclusion zone overlap — see flags. "
                "Confirm with the relevant agency before proceeding."
            )

        needs_review.append(
            "5-year continuous agricultural use is not verifiable from "
            "this data source — confirm with the county Property Appraiser."
        )
        needs_review.append(
            "Conservation easement status is not covered by any "
            "statewide GIS layer found during research — search the "
            "county Clerk/Recorder directly."
        )
        if parcel.jurisdiction is not None:
            needs_review.append(
                f"Jurisdiction field present ('{parcel.jurisdiction}') but "
                f"NOT yet enforced as a hard unincorporated-only filter — "
                f"confirm this parcel is unincorporated before proceeding."
            )
        else:
            needs_review.append(
                "This county's parcel layer has no confirmed jurisdiction "
                "field — unincorporated status cannot be checked from this "
                "data source at all; confirm manually."
            )

        centroid_lat: Optional[float] = None
        centroid_lon: Optional[float] = None
        if parcel.geometry is not None:
            try:
                centroid = get_centroid_lat_lon(parcel.geometry, source_wkid=3086)
                if centroid is not None:
                    centroid_lat, centroid_lon = centroid
            except ImportError:
                needs_review.append(
                    "pyproj not installed — map coordinates could not be "
                    "computed for this parcel. Run: pip install pyproj"
                )

        score, breakdown = scoring.score_candidate(
            acreage=parcel.acreage,
            pct_perimeter_qualifying=pct_qualifying,
            pathway_count=len(pathways),
            adjacent_to_interstate=False,
            adjacent_to_usb=False,
        )

        rows.append(ScanResultRow(
            parcel_id=parcel.parcel_id,
            county_id=county_id,
            acreage=parcel.acreage,
            acreage_source=parcel.acreage_source,
            owner_name=parcel.owner_name,
            owner_name_2=parcel.owner_name_2,
            use_code=parcel.use_code,
            jurisdiction=parcel.jurisdiction,
            pct_perimeter_qualifying=pct_qualifying,
            likely_pathways=pathways,
            exclusion_flags=exclusion_flags,
            attractiveness_score=score,
            score_breakdown=breakdown,
            needs_manual_review=needs_review,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
        ))

    rows.sort(key=lambda r: (r.attractiveness_score or 0), reverse=True)
    return rows


def _buffer_esri_geometry(geometry: dict, distance_feet: float) -> dict:
    """
    Buffer an ArcGIS-format polygon geometry outward by a fixed
    distance, returning a new ArcGIS-format geometry suitable for use
    as a spatial filter in a subsequent query. Requires Shapely.
    """
    from encirclement import esri_json_to_shapely  # local import to keep the ImportError path isolated

    poly = esri_json_to_shapely(geometry)
    buffered = poly.buffer(distance_feet)
    exterior_coords = list(buffered.exterior.coords)
    return {
        "rings": [exterior_coords],
        "spatialReference": geometry.get("spatialReference", {"wkid": 2236}),
    }


def rows_to_dicts(rows: list[ScanResultRow]) -> list[dict]:
    """Flatten for JSON serialization / CSV export."""
    return [asdict(r) for r in rows]
