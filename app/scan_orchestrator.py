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

from dataclasses import dataclass, asdict, field
from typing import Optional

from county_registry import COUNTIES
from parcel_fetcher import fetch_candidate_parcels, CandidateParcel, AREA_SR
from encirclement import compute_encirclement, determine_pathways, EncirclementResult, get_centroid_lat_lon
from arcgis_client import query_layer
import exclusions
import flu_taxonomy
import flwmi_client
import roads_client
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
    zcta5: Optional[str] = None
    # Master tier ranking (2026-07-06 pass). Overlaps with confidence_tier
    # by design during the transition -- both are populated so the old UI
    # keeps working while the new tier-driven UI is being wired in.
    tier: str = "unlikely"  # excluded / confirmed_qualifying / strong_candidate / watch_list / unlikely
    driving_pathways: list[str] = field(default_factory=list)
    # Kept on the row so tier can be recomputed later without re-running
    # the pipeline (persisted in coverage_ledger as part of the
    # master property database).
    interstate_frontage_pct: Optional[float] = None
    usb_perimeter_pct: Optional[float] = None
    adjacent_to_interstate: bool = False
    adjacent_to_usb: bool = False


def run_county_scan(
    county_id: str,
    min_acreage: float = 20.0,
    max_acreage: float = 4480.0,
    fetch_neighbor_buffer_feet: float = 50.0,
    max_candidates: int = 25,
    require_single_owner: bool = False,
    min_encirclement_pct: Optional[float] = None,
    flum_character_filter: Optional[str] = None,
    surrounding_density_filter: Optional[str] = None,
    zcta_geometry: Optional[dict] = None,
    zcta5: Optional[str] = None,
    skip_parcel_ids: Optional[set] = None,
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
        zcta_geometry=zcta_geometry,
        skip_parcel_ids=skip_parcel_ids,
    )

    rows: list[ScanResultRow] = []

    for parcel in candidates:
        needs_review: list[str] = []
        pathways: list[int] = []
        pct_qualifying: Optional[float] = None
        flum_character: Optional[str] = None
        surrounding_density = "unknown"
        adjacent_to_interstate = False
        adjacent_to_usb = False

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
                interstate_frontage_pct = 0.0
                usb_perimeter_pct = 0.0
                try:
                    adjacent_to_interstate = roads_client.check_adjacent_to_interstate(
                        parcel.geometry, county.name
                    )
                    if adjacent_to_interstate and encirclement.total_perimeter > 0:
                        frontage_m = roads_client.measure_interstate_frontage_meters(
                            parcel.geometry, county.name
                        )
                        interstate_frontage_pct = min(
                            100.0, frontage_m / encirclement.total_perimeter * 100.0
                        )
                except Exception as exc:  # noqa: BLE001 — a roads-layer failure shouldn't sink the whole candidate
                    adjacent_to_interstate = False
                    interstate_frontage_pct = 0.0
                    needs_review.append(f"Interstate-adjacency check failed to run: {exc}")
                try:
                    adjacent_to_usb = roads_client.check_adjacent_to_usb(
                        parcel.geometry, county.rural_area_layer_url
                    )
                    if county.rural_area_layer_url is not None and encirclement.total_perimeter > 0:
                        usb_m = roads_client.measure_usb_perimeter_meters(
                            parcel.geometry, county.rural_area_layer_url
                        )
                        usb_perimeter_pct = min(
                            100.0, usb_m / encirclement.total_perimeter * 100.0
                        )
                except Exception as exc:  # noqa: BLE001 — a roads-layer failure shouldn't sink the whole candidate
                    adjacent_to_usb = False
                    usb_perimeter_pct = 0.0
                    needs_review.append(f"Urban-service-area adjacency check failed to run: {exc}")
                if county.rural_area_layer_url is not None:
                    needs_review.append(
                        "Urban-service-area adjacency (used for encirclement Options C/D) is "
                        "approximated from this county's own Rural Area boundary, not a direct "
                        "USB layer — confirm with the Planning Department before relying on an "
                        "Option C/D match."
                    )
                # Option 5 (s. 163.3164(4)(c)3, F.S.): "located within the
                # boundary of an established rural study area adopted in the
                # local government's comprehensive plan which was intended
                # to be developed with residential uses." Verified per-county
                # 2026-07-06 via direct comp-plan review (not GIS search):
                # - Pasco: Northeast Pasco Rural Area is preservation-oriented
                #   (concurrent boundary amendment required for higher density
                #   applications). Not a (c)3 area. -> False.
                # - Nassau: 2030 plan discourages rural development, 2050
                #   vision preserves rural character. Not a (c)3 area. -> False.
                # - St. Johns: 2050 plan's Rural/Silviculture and Agricultural-
                #   Intensive designations are preservation, not future-
                #   residential. Not a (c)3 area. -> False.
                # - Osceola: has an 8,517-acre "study area" for Mixed-Use
                #   Districts 5 & 6, drafted for 14,010 residential units --
                #   BUT described in the county's own materials as inside
                #   the county's "urban service area," not currently a rural
                #   area transitioning to residential. Ambiguous under the
                #   statutory definition. Confirming with Osceola Planning
                #   before wiring True; conservatively False for now to avoid
                #   a false positive.
                # All four are False today. When this changes (either an
                # Osceola confirmation, or a new county added to the registry
                # that has a real (c)3 area), wire the per-county True/False
                # here via a CountyEndpoint field + a boundary check.
                inside_rural_study_area = False

                pathways = determine_pathways(
                    encirclement,
                    acreage=parcel.acreage or 0,
                    adjacent_to_interstate=adjacent_to_interstate,
                    adjacent_to_usb=adjacent_to_usb,
                    inside_rural_study_area=inside_rural_study_area,
                    interstate_frontage_pct=interstate_frontage_pct,
                    usb_perimeter_pct=usb_perimeter_pct,
                )
                if not pathways:
                    needs_review.append(
                        "No pathway matched automatically. This often means "
                        "the parcel relies on Option 3 (interstate + USB, "
                        "s. 163.3164(4)(c)1.c) or Option 4 (<=700 ac + USB "
                        "combination, s. 163.3164(4)(c)2), both of which have "
                        "known under-specifications (see STATUS.md), or on "
                        "Option 5 (rural study area, s. 163.3164(4)(c)3), "
                        "which none of the four pilot counties currently has "
                        "adopted based on direct comp-plan review. Don't "
                        "treat this as a definitive disqualification."
                    )
                if county_id == "osceola":
                    needs_review.append(
                        "Osceola specifically: Option 5 (rural study area, "
                        "s. 163.3164(4)(c)3) is treated as False here, but the "
                        "county has an 8,517-acre Mixed-Use District 5/6 "
                        "study area with 14,010 planned residential units "
                        "that may qualify. Confirm with Osceola Planning "
                        "whether this parcel falls within that boundary."
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

        # Acreage exception (s. 163.3164(4)(e), F.S.): the general 1,280-acre
        # cap rises to 4,480 acres if the parcel is surrounded on at least
        # 75% of its perimeter by existing or authorized residential
        # development at a buildout density >=1,000 residents/sq mi. The
        # buildout-density part requires per-county FLU-category residents-
        # per-sq-mi coefficients this project does not currently have, so
        # this can't be automated end-to-end -- surface the exception-
        # eligible parcels with a specific manual-review note instead of
        # silently dropping them (the pre-2026-07-06 behavior) or silently
        # counting them (which would over-include).
        if parcel.acreage is not None and parcel.acreage > 1280.0:
            if (pct_qualifying or 0) >= 75:
                needs_review.append(
                    f"Parcel exceeds the 1,280-acre general cap ({parcel.acreage:.0f} ac) "
                    "and passes the 75% perimeter test — it may qualify for the "
                    "urban/dense exception raising the cap to 4,480 ac (s. 163.3164(4)(e), "
                    "F.S.), but only if the surrounding 75% is specifically RESIDENTIAL "
                    "development at a buildout density of at least 1,000 residents/sq mi. "
                    "Neither the residential-only breakdown nor buildout density is "
                    "automated here — confirm with the county Planning Department before "
                    "relying on this parcel qualifying."
                )
            else:
                exclusion_flags.append(
                    f"Parcel exceeds the 1,280-acre general cap ({parcel.acreage:.0f} ac) "
                    "and does not meet the 75% perimeter test required for the urban/dense "
                    "exception (s. 163.3164(4)(e), F.S.) -- statutorily ineligible."
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
            adjacent_to_interstate=adjacent_to_interstate,
            adjacent_to_usb=adjacent_to_usb,
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
            pct_perimeter_qualifying=pct_qualifying,
        )
        tier, driving_pathways = scoring.assign_master_tier(
            exclusion_flags=exclusion_flags,
            likely_pathways=pathways,
            pct_perimeter_qualifying=pct_qualifying,
            interstate_frontage_pct=interstate_frontage_pct,
            usb_perimeter_pct=usb_perimeter_pct,
            acreage=parcel.acreage,
            adjacent_to_interstate=adjacent_to_interstate,
            adjacent_to_usb=adjacent_to_usb,
            single_owner_signal=parcel.single_owner_signal,
            sold_since_2025=parcel.sold_since_2025,
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
            zcta5=zcta5,
            tier=tier,
            driving_pathways=driving_pathways,
            interstate_frontage_pct=interstate_frontage_pct,
            usb_perimeter_pct=usb_perimeter_pct,
            adjacent_to_interstate=adjacent_to_interstate,
            adjacent_to_usb=adjacent_to_usb,
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
