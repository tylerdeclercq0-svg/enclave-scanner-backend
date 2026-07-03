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

    # Prefer a county-specific parcel layer over the statewide one when
    # available — see COUNTY_SPECIFIC_PARCEL_LAYERS above for why. This
    # routes to a separate function since the field schema genuinely
    # differs between the two data sources (DORUSECODE numeric vs.
    # DOR_UC 4-char string, different owner/location field names).
    if county_id in COUNTY_SPECIFIC_PARCEL_LAYERS:
        return _fetch_from_county_specific_layer(
            county_id=county_id,
            min_acreage=min_acreage,
            max_acreage=max_acreage,
            max_candidates=max_candidates,
        )

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
    where = f"CO_NO = {county.fips} AND DOR_UC IN ({codes_list})"

    # STRATEGY CHANGE: previous attempts used resultOffset-based paging
    # (via query_layer), which Esri's own community support forum
    # confirms gets progressively slower and can time out against
    # large tables (documented for an 8.8M-row table; the statewide
    # cadastral layer here has 10.8M rows) — this matches exactly what
    # was observed (504 Gateway Timeout even on a single-record probe).
    # The documented fix is to fetch matching OBJECTIDs first (a cheap,
    # indexed operation via returnIdsOnly), then fetch actual data in
    # small OBJECTID-range batches instead of resultOffset — see
    # query_layer_ids / query_layer_by_id_batches in arcgis_client.py.
    try:
        matching_ids = query_layer_ids(STATEWIDE_CADASTRAL_URL, where=where)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not fetch matching OBJECTIDs from the statewide "
            f"cadastral layer for county {county_id}: {exc}"
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
