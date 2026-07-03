"""
Pull candidate parcels for a county from the statewide DOR cadastral
layer, filtered by agricultural use code, acreage range, and (best
effort) single ownership.

This is the "Run scan" step from the UI — it does NOT do the perimeter
encirclement analysis (that needs the FLUM layer and real geometry
operations, handled in encirclement.py). This module's job is narrowing
10.8 million statewide parcels down to a county-sized candidate set fast,
using attribute filters only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from arcgis_client import query_layer, query_layer_ids, query_layer_by_id_batches
from county_registry import COUNTIES, STATEWIDE_CADASTRAL_URL, DOR_AGRICULTURAL_UC_RANGE


# Confirmed live FDOT-maintained statewide county boundary layer, with
# a NAME field (values like 'HILLSBOROUGH') and only 67 rows total —
# a trivially fast, small lookup table, unlike the 10.8M-row cadastral
# layer. Used to fetch a county's boundary polygon for SPATIAL
# filtering of the cadastral layer, since attribute filtering by CO_NO
# was confirmed (via live diagnostic testing) to time out no matter
# how it's queried — CO_NO itself appears unindexed on that layer.
# Spatial queries typically use a maintained spatial index and are a
# fundamentally different, usually much faster, query path.
COUNTY_BOUNDARY_LAYER_URL = (
    "https://gis.fdot.gov/arcgis/rest/services/Admin_Boundaries/"
    "FeatureServer/5"
)


def fetch_county_boundary_geometry(county_name: str) -> Optional[dict]:
    """
    Fetch a county's boundary polygon from the small (67-row) FDOT
    county boundary layer, for use as a spatial filter against the
    much larger statewide cadastral layer. county_name should be the
    plain county name (e.g. "HILLSBOROUGH") — exact casing/format not
    yet confirmed against live data; may need adjustment (title case,
    "County" suffix, etc.) once tested.

    CHANGED: pyproj (previously used to reproject this layer's native
    WKID 26917 / UTM 17N geometry to the statewide cadastral layer's
    WKID 3086 / Florida Albers) failed to initialize on Render with a
    "TypeError: expected bytes, str found" — a known category of
    pyproj/PROJ deployment fragility (its compiled C dependency, PROJ,
    can fail to locate its coordinate system data files depending on
    how the Python environment is set up). Rather than fight that
    dependency further, this now requests the geometry directly in
    WGS84 (WKID 4326, plain lat/lon) via the outSR parameter — ArcGIS
    Server performs this specific reprojection server-side reliably
    (it's the single most common SR conversion any GIS service
    supports), removing the need for pyproj or a hand-written UTM
    transform. The hand-written Albers projection math needed to go
    from lat/lon to the statewide layer's native SR is implemented in
    _latlon_to_florida_albers below using published, verified
    projection parameters — simpler and more reliable than a full
    UTM-to-Albers conversion would have been.
    """
    where = f"UPPER(NAME) = '{county_name.upper()}'"
    features = list(query_layer(
        COUNTY_BOUNDARY_LAYER_URL,
        where=where,
        out_fields="NAME,FIPS",
        return_geometry=True,
        page_size=1,
        out_sr=4326,
    ))
    if not features:
        return None
    geometry = features[0].get("geometry")
    if geometry is None:
        return None
    if "spatialReference" not in geometry:
        geometry["spatialReference"] = {"wkid": 4326}

    reprojected = _reproject_latlon_geometry_to_florida_albers(geometry)

    # SIMPLIFICATION: the full boundary polygon has 5,013 real coordinate
    # points (confirmed via live diagnostic), and a spatial query using
    # that much detail against the 10.8M-row statewide layer still times
    # out even at 12 seconds, even though the same query structurally
    # succeeds instantly for other requests. Rather than a precise
    # county outline, use a simple rectangular bounding envelope
    # instead — coarser (it will include a thin margin of neighboring
    # counties along the boundary), but should be dramatically faster
    # for the server to evaluate. This is an acceptable trade-off for a
    # SCREENING tool: a few extra out-of-county candidates in the
    # results are easy for a human to spot and discard, whereas a
    # non-functional scan is not.
    return _bounding_envelope(reprojected)


def _bounding_envelope(geometry: dict) -> dict:
    """
    Reduce a detailed polygon to its rectangular bounding envelope
    (min/max x and y), trading spatial precision for query speed. See
    the comment at the call site above for why this trade-off is
    reasonable for a screening tool.
    """
    all_x = [pt[0] for ring in geometry.get("rings", []) for pt in ring]
    all_y = [pt[1] for ring in geometry.get("rings", []) for pt in ring]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    return {
        "rings": [[
            [xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin],
        ]],
        "spatialReference": geometry.get("spatialReference", {"wkid": 3086}),
    }


def _reproject_latlon_geometry_to_florida_albers(geometry: dict) -> dict:
    """
    Convert a WGS84 (lat/lon) polygon geometry to Florida Albers Equal
    Area Conic (the statewide cadastral layer's native SR, WKID 3086),
    using the published projection parameters for this exact CRS
    (confirmed via Esri's own support documentation for UTM-to-Albers
    Florida conversions): central meridian -84.0°, standard parallels
    24.0° and 31.5°, latitude of origin 24.0°, false easting 400,000m,
    false northing 0m, on the GRS 1980 ellipsoid.

    This hand-implements the standard Albers Equal-Area Conic forward
    projection formula (a well-documented, non-controversial piece of
    cartographic math) rather than depending on pyproj, after pyproj
    was confirmed to fail to initialize in this deployment environment.
    """
    import math

    # GRS 1980 ellipsoid parameters (matches the statewide layer's datum)
    a = 6378137.0  # semi-major axis, meters
    f = 1 / 298.257222101  # flattening
    e2 = 2 * f - f * f  # eccentricity squared
    e = math.sqrt(e2)

    lat0 = math.radians(24.0)
    lon0 = math.radians(-84.0)
    lat1 = math.radians(24.0)
    lat2 = math.radians(31.5)
    false_easting = 400000.0
    false_northing = 0.0

    def m(lat):
        sin_lat = math.sin(lat)
        return math.cos(lat) / math.sqrt(1 - e2 * sin_lat * sin_lat)

    def q(lat):
        sin_lat = math.sin(lat)
        return (1 - e2) * (
            sin_lat / (1 - e2 * sin_lat * sin_lat)
            - (1 / (2 * e)) * math.log((1 - e * sin_lat) / (1 + e * sin_lat))
        )

    m1, m2 = m(lat1), m(lat2)
    q0, q1, q2 = q(lat0), q(lat1), q(lat2)

    n = (m1 * m1 - m2 * m2) / (q2 - q1)
    c = m1 * m1 + n * q1
    rho0 = a * math.sqrt(c - n * q0) / n

    def project(lon_deg: float, lat_deg: float) -> tuple[float, float]:
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        q_lat = q(lat)
        rho = a * math.sqrt(c - n * q_lat) / n
        theta = n * (lon - lon0)
        x = false_easting + rho * math.sin(theta)
        y = false_northing + rho0 - rho * math.cos(theta)
        return x, y

    new_rings = []
    for ring in geometry.get("rings", []):
        new_ring = [list(project(point[0], point[1])) for point in ring]
        new_rings.append(new_ring)

    return {
        "rings": new_rings,
        "spatialReference": {"wkid": 3086},
    }


@dataclass
class CandidateParcel:
    parcel_id: str
    county_fips: int
    acreage: Optional[float]
    dor_use_code: Optional[str]
    owner_name: Optional[str]
    owner_addr_city: Optional[str]
    owner_addr_state: Optional[str]
    just_value: Optional[float]
    classified_ag_value: Optional[float]
    sale_year: Optional[int]
    sale_price: Optional[float]
    section: Optional[str]
    township: Optional[str]
    range_: Optional[str]
    legal_desc: Optional[str]
    geometry: Optional[dict]


# Acreage on the statewide cadastral layer isn't a direct field — it's
# derived from LND_SQFOOT (land square footage) when LND_UNTS_C indicates
# the units are in square feet rather than acres or front feet. This
# conversion must be applied per record, not assumed.
SQFT_PER_ACRE = 43560.0


def _acreage_from_record(attrs: dict) -> Optional[float]:
    """
    Convert LND_SQFOOT to acres. LND_UNTS_C carries a DOR-defined unit
    code; confirm its exact coded values for "square feet" against a
    live record before trusting this blindly — different counties have
    historically had inconsistent unit code reporting in the NAL extract.
    """
    sqft = attrs.get("LND_SQFOOT")
    if sqft is None:
        return None
    try:
        return round(float(sqft) / SQFT_PER_ACRE, 1)
    except (TypeError, ValueError):
        return None


# NOTE: county-specific SWFWMD regional parcel layers (previously
# wired in here for Hillsborough/Sarasota) were found to be
# unreliable during live testing — sibling layers on the same service
# (Pinellas County Parcels, WMISViewer) return "Could not access any
# server machines" errors, and even a WHERE 1=1 query against the
# Hillsborough layer itself returned zero features despite the layer's
# metadata looking valid. This points to a retired/partially-migrated
# backend behind a still-responsive metadata endpoint — not something
# to build on. Reverted to the statewide layer, fixed properly this
# time using OBJECTID-batch fetching instead of resultOffset paging
# (see query_layer_by_id_batches in arcgis_client.py) to avoid the
# documented ArcGIS performance cliff on multi-million-row tables.
COUNTY_SPECIFIC_PARCEL_LAYERS: dict[str, str] = {}


def fetch_candidate_parcels(
    county_id: str,
    min_acreage: float = 20.0,
    max_acreage: float = 1280.0,
    uc_range: tuple[int, int] = DOR_AGRICULTURAL_UC_RANGE,
    require_single_owner_signal: bool = True,
    max_candidates: int = 200,
) -> list[CandidateParcel]:
    """
    Query the statewide cadastral layer for parcels in one county that
    plausibly meet the acreage and agricultural-use criteria.

    max_candidates caps how many parcels this pulls WITH geometry. This
    matters because acreage isn't a stored, queryable field on this
    layer (it's derived from LND_SQFOOT after the fact), so the acreage
    filter can't be pushed down into the ArcGIS WHERE clause — every
    matching DOR_UC parcel in the county has to be fetched and checked
    client-side. For a large county this can be thousands of parcels;
    pulling full polygon geometry for all of them in one request is what
    caused the initial timeout against the live server. Capping at 200
    keeps a first real-world scan fast; raise this once scan performance
    has been profiled against real response times, and consider fetching
    attributes-only first (return_geometry=False) to do the acreage
    filter, then a second geometry-only fetch for just the survivors —
    a cheaper two-pass approach not yet implemented here.

    Notes on what this filter CAN and CANNOT determine on its own:
      - Acreage cap and DOR use code: directly filterable (acreage is
        filtered client-side after fetch, as described above).
      - "Single owner/entity": the cadastral layer has no concept of
        multi-parcel ownership clusters. This function can only filter
        on owner name patterns it can not group adjacent parcels under
        common ownership across a multi-parcel enclave. Real single-
        ownership/control verification still requires a title search,
        exactly as flagged in the UI's checklist.
      - 5-year continuous agricultural use: not available at all in this
        layer. JV_CLASS_U (just value, classified use) and AV_CLASS_U
        being non-null/non-zero are a weak proxy — they indicate the
        parcel currently carries an agricultural classification for tax
        purposes, but say nothing about duration.
    """
    county = COUNTIES.get(county_id)
    if county is None:
        raise ValueError(f"Unknown county id: {county_id}")

    # Trimmed to ONLY fields directly confirmed to exist on this exact
    # layer's live metadata response (CO_NO, PARCEL_ID, DOR_UC, OWN_NAME,
    # LND_SQFOOT, JV, LND_UNTS_C). Several other fields used in an
    # earlier version (S_LEGAL, SEC, TWN, RNG, SALE_YR1, SALE_PRC1,
    # JV_CLASS_U, OWN_CITY, OWN_STATE) were assumed from a similar-
    # looking NAL schema but were never individually confirmed against
    # this specific FeatureServer, and an invalid field name in
    # outFields can produce the same generic "Invalid query parameters"
    # 400 error as a bad WHERE clause — making it impossible to tell
    # which one was actually wrong from the error message alone. Once a
    # scan succeeds with this trimmed field list, add the other fields
    # back ONE AT A TIME (or fetch the layer's full /FeatureServer/0
    # metadata directly) to identify their real names before re-adding.
    out_fields = ",".join([
        "PARCEL_ID", "CO_NO", "DOR_UC", "LND_SQFOOT", "LND_UNTS_C",
        "OWN_NAME", "JV",
    ])

    # CHANGED: the >= / <= range comparison on DOR_UC (a string field)
    # caused a 504 Gateway Timeout even with resultRecordCount=1 —
    # string range comparisons force a slow scan rather than an
    # indexed lookup on this server. Switched to an IN(...) list of
    # specific common agricultural codes instead, which is typically
    # much faster since it can use equality matching per value. This
    # list is NOT exhaustive of the full 5000-6999 agricultural range —
    # it covers the most common real-world codes (pasture, grove,
    # cropland, timber per Lee County's published DOR code list) as a
    # starting point to get a working query, not a complete substitute
    # for the full range. Expand this list once a query succeeds and
    # response times are understood.
    common_ag_codes = [
        "5100", "5200", "5300", "5400", "5401", "5375", "5380",
        "6000", "6010", "6011", "6012", "6100", "6110", "6200", "6210",
        "6300", "6400", "6410", "6500",
        "6611", "6615", "6620", "6630", "6645", "6650", "6655", "6665", "6675",
    ]
    codes_list = ",".join(f"'{c}'" for c in common_ag_codes)

    # FIX (confirmed via live diagnostic testing): CO_NO alone times
    # out against the statewide cadastral layer — it appears unindexed,
    # forcing a full scan of 10.8M rows regardless of what else is in
    # the WHERE clause. Attribute-based county filtering on this layer
    # is not viable. Switched to a SPATIAL filter instead: fetch the
    # county's boundary polygon from a small, fast reference layer
    # (67 rows), then use it as a geometry filter against the
    # cadastral layer. Spatial queries typically hit a maintained
    # spatial index and behave very differently, performance-wise,
    # from attribute filters on this same table.
    try:
        boundary_geometry = fetch_county_boundary_geometry(county.name)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"[STEP: fetch boundary] Failed fetching "
            f"{county.name} County's boundary polygon from the FDOT "
            f"reference layer: {type(exc).__name__}: {exc}"
        )
    if boundary_geometry is None:
        raise RuntimeError(
            f"[STEP: fetch boundary] No boundary polygon found for "
            f"{county.name} County from {COUNTY_BOUNDARY_LAYER_URL} — "
            f"check the NAME field's exact format on that layer (case, "
            f"'County' suffix, etc.) against a live query."
        )

    # DOR_UC filtering still applies as an attribute condition, but now
    # combined with a spatial constraint rather than being the sole
    # filter — this may still be slow if DOR_UC itself is unindexed;
    # not yet confirmed independently of CO_NO. If this still times
    # out, try the spatial filter with where="1=1" (no DOR_UC at all)
    # to isolate whether DOR_UC alone is now the bottleneck.
    where = f"DOR_UC IN ({codes_list})"

    # DIAGNOSTIC (re-run after replacing pyproj with hand-written,
    # verified Albers projection math): confirmed boundary geometry is
    # real (49 rings, 5013 points) and the reprojection math round-trips
    # correctly against a known point (Tampa, FL). This tests whether
    # the actual spatial query against the statewide layer succeeds now.
    #
    # BUG FOUND AND FIXED: this block's own diagnostic "success" report
    # was raised as a plain RuntimeError, and the exception handler
    # below had `except RuntimeError: raise` to avoid re-wrapping that
    # intentional report — but arcgis_client.ArcGISQueryError (raised on
    # a REAL timeout/failure) is ALSO a RuntimeError subclass. This
    # meant real timeouts were being caught by that `except RuntimeError`
    # clause and re-raised completely unchanged, silently bypassing the
    # [STEP: spatial query] label every single time — confirmed via a
    # full traceback showing the raw ArcGISQueryError reaching main.py
    # unlabeled. Fixed by checking for a specific sentinel exception
    # type for the diagnostic's own intentional report, instead of the
    # overly broad RuntimeError.
    class _DiagnosticReport(Exception):
        pass

    try:
        spatial_only_ids = query_layer_ids(
            STATEWIDE_CADASTRAL_URL,
            where="1=1",
            geometry=boundary_geometry,
            geometry_type="esriGeometryPolygon",
            spatial_rel="esriSpatialRelIntersects",
        )
        raise _DiagnosticReport(
            f"[STEP: spatial query] DIAGNOSTIC: spatial "
            f"filter alone (no DOR_UC) matched "
            f"{len(spatial_only_ids)} parcels inside the {county.name} "
            f"County boundary after client-side reprojection to WKID "
            f"3086. If this number is large and reasonable (Hillsborough "
            f"has roughly 400,000+ real parcels), the spatial filter "
            f"finally works and the DOR_UC IN (...) code list is the "
            f"next thing to verify. If still 0, the reprojection logic "
            f"itself has a bug, or spatialRel/geometryType needs "
            f"adjustment."
        )
    except _DiagnosticReport as report:
        raise RuntimeError(str(report))
    except Exception as exc:  # noqa: BLE001 — this now correctly catches real failures (including ArcGISQueryError) instead of having them slip through an overly broad `except RuntimeError: raise`
        raise RuntimeError(
            f"[STEP: spatial query] Spatial-only query "
            f"against the statewide cadastral layer failed outright: "
            f"{type(exc).__name__}: {exc}"
        )

    try:
        matching_ids = query_layer_ids(
            STATEWIDE_CADASTRAL_URL,
            where=where,
            geometry=boundary_geometry,
            geometry_type="esriGeometryPolygon",
            spatial_rel="esriSpatialRelIntersects",
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not fetch matching OBJECTIDs from the statewide "
            f"cadastral layer for county {county_id} using a spatial "
            f"filter: {exc}"
        )

    if not matching_ids:
        return []

    # Only fetch full attribute data for a bounded sample of matching
    # IDs, not all of them — for a large county the agricultural-code
    # match set could still be in the thousands, and pulling full
    # attributes for all of them isn't necessary just to find
    # max_candidates worth of acreage-qualifying parcels. This is a
    # real trade-off: parcels beyond this sample size are never
    # considered, even if they'd otherwise qualify. A production
    # version should page through matching_ids in batches until
    # max_candidates survivors are found, not stop after one fixed
    # sample — not yet implemented here.
    id_sample = matching_ids[:max(max_candidates * 5, 100)]

    out_fields = ",".join([
        "OBJECTID", "PARCEL_ID", "CO_NO", "DOR_UC", "LND_SQFOOT",
        "LND_UNTS_C", "OWN_NAME", "JV",
    ])

    attrs_only_features = list(query_layer_by_id_batches(
        STATEWIDE_CADASTRAL_URL,
        object_ids=id_sample,
        out_fields=out_fields,
        return_geometry=False,
        batch_size=200,
    ))

    # Apply the acreage filter BEFORE fetching any geometry — this is
    # the actual point of the two-pass split: only pull expensive
    # polygon geometry for the small number of parcels that survive
    # the cheap attribute filter, instead of fetching geometry for
    # every agricultural parcel in the county up front.
    surviving_attrs = []
    for feat in attrs_only_features:
        attrs = feat.get("attributes", {})
        acreage = _acreage_from_record(attrs)
        if acreage is not None and not (min_acreage <= acreage <= max_acreage):
            continue
        surviving_attrs.append((attrs, acreage))
        if len(surviving_attrs) >= max_candidates:
            break

    # PASS 2: fetch geometry only for surviving parcels, one at a time
    # by OBJECTID (cheaper/more reliable than PARCEL_ID string matching
    # for a single-row lookup). This trades more individual requests
    # for much smaller/cheaper individual responses — appropriate here
    # since surviving_attrs is capped at max_candidates (small).
    candidates: list[CandidateParcel] = []
    for attrs, acreage in surviving_attrs:
        parcel_id = attrs.get("PARCEL_ID")
        object_id = attrs.get("OBJECTID")
        geometry = None
        if object_id is not None:
            try:
                geom_features = list(query_layer(
                    STATEWIDE_CADASTRAL_URL,
                    where=f"OBJECTID = {object_id}",
                    out_fields="OBJECTID",
                    return_geometry=True,
                    page_size=1,
                ))
                if geom_features:
                    geometry = geom_features[0].get("geometry")
            except Exception:  # noqa: BLE001 — a single parcel's geometry failing shouldn't abort the whole scan; it just gets flagged downstream as "no geometry, verify manually" per scan_orchestrator.py's existing handling
                geometry = None

        candidates.append(CandidateParcel(
            parcel_id=attrs.get("PARCEL_ID"),
            county_fips=county.fips,
            acreage=acreage,
            dor_use_code=attrs.get("DOR_UC"),
            owner_name=attrs.get("OWN_NAME"),
            owner_addr_city=attrs.get("OWN_CITY"),
            owner_addr_state=attrs.get("OWN_STATE"),
            just_value=attrs.get("JV"),
            classified_ag_value=attrs.get("JV_CLASS_U"),
            sale_year=attrs.get("SALE_YR1"),
            sale_price=attrs.get("SALE_PRC1"),
            section=attrs.get("SEC"),
            township=attrs.get("TWN"),
            range_=attrs.get("RNG"),
            legal_desc=attrs.get("S_LEGAL"),
            geometry=geometry,
        ))

        if len(candidates) >= max_candidates:
            break

    return candidates


def group_by_apparent_owner(parcels: list[CandidateParcel]) -> dict[str, list[CandidateParcel]]:
    """
    Best-effort grouping of adjacent/nearby parcels that share an exact
    owner name string, as a starting point for identifying multi-parcel
    enclaves under common control. This is NOT a substitute for a title
    search: LLCs, trusts, and family entities frequently hold contiguous
    land under slightly different name variants (e.g. "Smith Family
    Trust" vs "Smith Family LLC"), which this naive grouping will miss.
    """
    grouped: dict[str, list[CandidateParcel]] = {}
    for p in parcels:
        key = (p.owner_name or "UNKNOWN").strip().upper()
        grouped.setdefault(key, []).append(p)
    return grouped
