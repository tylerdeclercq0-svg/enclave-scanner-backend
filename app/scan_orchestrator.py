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
from parcel_fetcher import fetch_candidate_parcels, CandidateParcel, AREA_SR
from encirclement import compute_encirclement, determine_pathways, EncirclementResult, get_centroid_lat_lon
from arcgis_client import query_layer
import exclusions
import flu_taxonomy
import flwmi_client
import scoring
import statutory_checks


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
    sold_since_2025: Optional[bool] = None
    single_owner_signal: Optional[bool] = None
    water_source: Optional[str] = None
    wastewater_method: Optional[str] = None
    water_sewer_confidence: str = "Unknown"
    flum_character: Optional[str] = None
    surrounding_density: str = "unknown"
    confidence_tier: str = "unlikely"


def run_county_scan(
    county_id: str,
    min_acreage: float = 20.0,
    max_acreage: float = 1280.0,
    fetch_neighbor_buffer_feet: float = 50.0,
    max_candidates: int = 25,
    require_single_owner: bool = False,
    min_encirclement_pct: Optional[float] = None,
    flum_character_filter: Optional[str] = None,
    surrounding_density_filter: Optional[str] = None,
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
        require_single_owner=require_single_owner,
    )

    rows: list[ScanResultRow] = []

    for parcel in candidates:
        needs_review: list[str] = []
        pathways: list[int] = []
        pct_qualifying: Optional[float] = None
        flum_character: Optional[str] = None
        surrounding_density = "unknown"

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
                    out_sr=AREA_SR,
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
                # FLUM character (candidate's own designation) + surrounding
                # density bucket (dominant neighboring designation) — both
                # reuse neighbor_features/segments already fetched above,
                # no new query. Best-effort classification, see
                # flu_taxonomy.py's own caveat.
                flum_character = flu_taxonomy.determine_own_flu(
                    parcel.geometry, neighbor_features, county.flu_field
                )
                dominant_neighbor_flu = flu_taxonomy.dominant_segment_flu(encirclement.segments)
                surrounding_density = flu_taxonomy.classify_density(dominant_neighbor_flu)
            except ImportError:
                needs_review.append(
                    "Shapely not installed in this environment — "
                    "encirclement test was skipped entirely."
                )
            except Exception as exc:  # noqa: BLE001 — surface any geometry/query failure to the reviewer rather than silently dropping the parcel
                needs_review.append(f"Encirclement test failed to run: {exc}")

        if min_encirclement_pct is not None and (pct_qualifying or 0) < min_encirclement_pct:
            continue
        if flum_character_filter and (flum_character or "").lower() != flum_character_filter.lower():
            continue
        if surrounding_density_filter and surrounding_density != surrounding_density_filter:
            continue

        exclusion_flags = exclusions.check_exclusions(parcel)

        is_unincorporated, unincorporated_detail = statutory_checks.check_unincorporated(
            county, parcel.geometry, AREA_SR
        )
        if is_unincorporated is False:
            exclusion_flags.append(
                f"Unincorporated-status hard filter FAILED: {unincorporated_detail}"
            )
        elif is_unincorporated is None:
            needs_review.append(f"Unincorporated-status check: {unincorporated_detail}")

        if exclusion_flags:
            needs_review.append(
                "Real statutory exclusion zone hit — see Exclusions. "
                "Confirm with the relevant agency before proceeding."
            )

        needs_review.append(
            "5-year continuous agricultural use is not verifiable from "
            "this data source — confirm with the county Property Appraiser."
        )
        # ACSC / conservation easement / military buffer reminders — always
        # present regardless of geometry or query results, distinct from a
        # real exclusion_flags hit. See exclusions.standing_manual_notes()
        # docstring for why these moved out of exclusion_flags.
        needs_review.extend(exclusions.standing_manual_notes())

        if parcel.sold_since_2025 is None:
            needs_review.append(
                "Post-1/1/2025 ownership-change status could not be "
                "determined from this county's sale-date field(s) — "
                "confirm manually before relying on single-owner-as-of-"
                "1/1/2025 eligibility."
            )
        elif parcel.sold_since_2025:
            needs_review.append(
                "Parcel's most recent recorded sale is on or after "
                "1/1/2025 — confirm this doesn't disqualify the "
                "single-owner-as-of-1/1/2025 pathway requirement."
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

        water_sewer = flwmi_client.WaterSewerResult(
            water_source=None, wastewater_method=None, confidence="Unknown", found=False
        )
        if parcel.parcel_id:
            try:
                water_sewer = flwmi_client.lookup_water_sewer(county, parcel.parcel_id)
            except Exception as exc:  # noqa: BLE001 — a water/sewer lookup failure shouldn't sink the whole candidate
                needs_review.append(f"Water/sewer service estimate lookup failed: {exc}")
        if not water_sewer.found:
            needs_review.append(
                "No FDOH Florida Water Management Inventory record found for "
                "this parcel — water/sewer service is unestimated, not "
                "confirmed absent. Confirm directly with the county utility."
            )

        confidence_tier = scoring.classify_confidence(
            likely_pathways=pathways,
            exclusion_flags=exclusion_flags,
            single_owner_signal=parcel.single_owner_signal,
            water_sewer_confidence=water_sewer.confidence,
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
            single_owner_signal=parcel.single_owner_signal,
            water_source=water_sewer.water_source,
            wastewater_method=water_sewer.wastewater_method,
            water_sewer_confidence=water_sewer.confidence,
            flum_character=flum_character,
            surrounding_density=surrounding_density,
            confidence_tier=confidence_tier,
            exclusion_flags=exclusion_flags,
            attractiveness_score=score,
            score_breakdown=breakdown,
            needs_manual_review=needs_review,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            sold_since_2025=parcel.sold_since_2025,
        ))

    rows.sort(key=lambda r: (r.attractiveness_score or 0), reverse=True)
    return rows


def _buffer_esri_geometry(geometry: dict, distance_feet: float) -> dict:
    """
    Buffer an ArcGIS-format polygon geometry outward by a fixed
    distance, returning a new ArcGIS-format geometry suitable for use
    as a spatial filter in a subsequent query. Requires Shapely.

    Hardcodes the output spatialReference to AREA_SR (3086, Florida
    Albers meters) rather than reading `geometry.get("spatialReference")`
    -- confirmed live that ArcGIS Server does NOT include a
    spatialReference on each feature's geometry in a /query response
    (it's only present once, at the FeatureSet root, which
    arcgis_client.query_layer discards). The old fallback default of
    wkid 2236 was silently wrong: every candidate geometry passed in
    here actually comes from parcel_fetcher.fetch_candidate_parcels,
    which always requests outSR=AREA_SR, so 3086 is the correct SR to
    assert here, not a guess. Getting this wrong caused every neighbor
    spatial query to silently return zero results (garbage
    coordinates interpreted under the wrong CRS), not a real "0%
    encircled" answer -- confirmed live against a real Pasco parcel.

    distance_feet is converted to meters before being passed to
    shapely's buffer() -- shapely operates on the polygon's raw
    coordinate values with no unit awareness, and since the geometry is
    now correctly asserted to be in AREA_SR (3086, meters, not feet
    despite the parameter's name/the original State-Plane-era
    assumption), passing the feet value straight through would silently
    buffer by that many METERS instead (about 3.3x larger than
    intended).
    """
    from encirclement import esri_json_to_shapely  # local import to keep the ImportError path isolated
    from shapely.geometry import MultiPolygon

    FEET_PER_METER = 0.3048
    distance_meters = distance_feet * FEET_PER_METER

    poly = esri_json_to_shapely(geometry)
    buffered = poly.buffer(distance_meters)
    # esri_json_to_shapely can now return a MultiPolygon for genuinely
    # multipart candidate geometry (see its 2026-07-03 fix) — buffering
    # that can still yield a MultiPolygon if the parts stay far enough
    # apart, so build a multi-ring Esri geometry (one ring per part's
    # exterior) instead of assuming a single `.exterior` always exists.
    if isinstance(buffered, MultiPolygon):
        rings = [list(part.exterior.coords) for part in buffered.geoms]
    else:
        rings = [list(buffered.exterior.coords)]
    return {
        "rings": rings,
        "spatialReference": {"wkid": AREA_SR},
    }


def rows_to_dicts(rows: list[ScanResultRow]) -> list[dict]:
    """Flatten for JSON serialization / CSV export."""
    return [asdict(r) for r in rows]
