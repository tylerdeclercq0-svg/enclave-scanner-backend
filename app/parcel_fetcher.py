"""
Pull candidate parcels for a county from that county's OWN parcel/
cadastral layer (see county_registry.CountyEndpoint.parcel_service_url),
filtered by agricultural use code and acreage.

REWRITTEN 2026-07-03: the previous version of this module queried the
statewide cadastral layer (Florida_Statewide_Cadastral) filtered by
CO_NO. Live testing confirmed CO_NO has no index on that hosted layer
and any predicate on it — attribute or spatial — times out (400 after
~55s, or a real 504) once the target county is more than a few positions
into the table's physical row order. See county_registry.py's
"GROUND-TRUTHED" docstring section for the full diagnostic history.
Each of the four target counties now has its own parcel layer, confirmed
live via describe_layer (?f=pjson) and at least one test query — see
each CountyEndpoint's `notes` field for what was actually verified vs.
carried over as an unconfirmed guess.

These county-specific tables are far smaller than the 10.8M-row
statewide layer (tens to a few hundred thousand rows), and live testing
showed sub-second response times for WHERE-filtered queries against all
four — there's no evidence of the same indexing problem here. This
means the OBJECTID-batch two-pass fetch strategy the old version used
specifically to work around the statewide layer's performance cliff is
no longer necessary: a single query with returnGeometry=true and a
server-side WHERE clause is fast enough at this table size, and is what
this version does.

ACREAGE FIELD VERIFICATION (2026-07-03): before trusting VAL_ACRES
(Pasco), ACRES (Nassau), and TotalAcres (Osceola) as literal acres, each
was cross-checked against an independently shoelace-computed polygon
area (geometry fetched with outSR=3086 — Florida GDL Albers, an
equal-area projection in meters, not the layers' native Web Mercator)
for one real parcel:
  - Pasco VAL_ACRES=18.85 vs. computed 18.82 acres (match)
  - Osceola TotalAcres=48.96 vs. computed 48.94 acres (match)
  - Nassau ACRES=646.341 vs. computed 646.34 acres (match, after fixing
    a bug in the FIRST verification attempt that only summed the
    polygon's first ring — this parcel has 2 rings, and Esri's polygon
    format requires summing SIGNED area across every ring, not just the
    exterior, to correctly handle multi-part parcels and holes)
All three fields are confirmed to be real acres, not square feet or
some other unit.

St. Johns has no populated acreage field on its parcel layer at all
(Shape_STArea__ returned 0.0 on every row sampled) — acreage there MUST
be computed from geometry. Also confirmed live: the layer's native SR is
Web Mercator (wkid 3857/102100), which is a projected CRS (not raw
lat/long) but is NOT equal-area — computing area directly in Web
Mercator meters overstates area by a real, measured 1.338x at this
layer's latitude (theoretical distortion at 30°N is 1/cos²(30°) = 1.333,
matching closely). Every geometry fetch in this module therefore
requests outSR=3086 explicitly, regardless of a layer's native SR, so
area math is always done in an equal-area projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from arcgis_client import query_layer
from county_registry import COUNTIES, CountyEndpoint
import statutory_checks


SQM_PER_ACRE = 4046.8564224

# The equal-area CRS every geometry fetch in this module requests via
# outSR, regardless of a layer's native spatial reference. Matches the
# statewide cadastral layer's native SR, so acreage/geometry from
# different counties is directly comparable.
AREA_SR = 3086


@dataclass
class CandidateParcel:
    parcel_id: Optional[str]
    county_id: str
    acreage: Optional[float]
    acreage_source: str  # "field" or "computed_from_geometry" — for auditing which path was used
    use_code: Optional[str]
    use_code_field: Optional[str]  # which field this came from, since it's a different field per county
    owner_name: Optional[str]
    owner_name_2: Optional[str]
    jurisdiction: Optional[str]
    geometry: Optional[dict]
    # True/False if a post-1/1/2025 sale is determinable from this
    # county's parcel-layer sale-date field(s), None if not (missing
    # field, unparseable value, or county has no sale_date_encoding set).
    sold_since_2025: Optional[bool] = None
    # True when owner_name_2 is empty (no co-owner recorded on this
    # parcel's own attributes), False when a second owner name IS
    # present, None when this county's parcel layer has no owner_field_2
    # at all (Nassau, St. Johns) -- unknowable, not assumed single-owner.
    # This is ONLY a name-matching signal on THIS parcel's own record,
    # not a real ownership/control lookup across a multi-parcel enclave
    # or a title search -- same caveat already noted above in
    # fetch_candidate_parcels()'s docstring.
    single_owner_signal: Optional[bool] = None


def _signed_ring_area(ring: list[list[float]]) -> float:
    """Shoelace formula, signed (not absolute) — sign encodes winding direction."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_area_acres(rings: list[list[list[float]]]) -> float:
    """
    Area of an Esri-format polygon (list of rings, coordinates in a
    projected/equal-area CRS such as wkid 3086), in acres.

    Sums SIGNED area across every ring rather than just the first
    (exterior) ring — required to correctly handle multi-part parcels
    (e.g. a single legal parcel with two disjoint boundary loops, seen
    live on a real Nassau County timberland parcel) and holes, per
    Esri's polygon ring-winding convention. Confirmed against three real
    parcels with known VAL_ACRES/ACRES/TotalAcres field values before
    being trusted for St. Johns, which has no acreage field at all.
    """
    if not rings:
        return 0.0
    total_sqm = abs(sum(_signed_ring_area(r) for r in rings))
    return total_sqm / SQM_PER_ACRE


# ---------------------------------------------------------------------------
# Per-county agricultural classification.
#
# Deliberately NOT one shared filter: Pasco and Nassau use a string RANGE
# comparison on a 3-char DOR-style code, while St. Johns and Osceola use
# county-local 4-char codes that must be matched against an EXPLICIT list,
# not a range — a naive range on Osceola's DORCode was confirmed live to
# silently match '0611' ("RETIREMENT HOMES") because that string sorts
# lexically between '050' and '069' despite being an unrelated code. Each
# function below encodes both the WHERE-clause fragment to push down to
# the server AND a client-side re-check against the fetched attributes,
# so a bad WHERE clause can never be the only line of defense.
# ---------------------------------------------------------------------------


def _pasco_ag_where(county: CountyEndpoint) -> str:
    lo, hi = county.parcel_agricultural_use_code_range
    return f"{county.parcel_use_code_field}>='{lo}' AND {county.parcel_use_code_field}<='{hi}'"


def _pasco_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("DIR_CLASS")
    if code is None:
        return False
    lo, hi = COUNTIES["pasco"].parcel_agricultural_use_code_range
    return lo <= code <= hi


def _nassau_ag_where(county: CountyEndpoint) -> str:
    lo, hi = county.parcel_agricultural_use_code_range
    clause = f"{county.parcel_use_code_field}>='{lo}' AND {county.parcel_use_code_field}<='{hi}'"
    if county.parcel_county_filter:
        clause = f"{county.parcel_county_filter} AND {clause}"
    return clause


def _nassau_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("DORUC")
    if code is None:
        return False
    lo, hi = COUNTIES["nassau"].parcel_agricultural_use_code_range
    return lo <= code <= hi


def _st_johns_ag_where(county: CountyEndpoint) -> str:
    codes = ",".join(f"'{c}'" for c in county.parcel_agricultural_use_codes)
    return f"{county.parcel_use_code_field} IN ({codes})"


def _st_johns_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("USE_CODE")
    return code in COUNTIES["st_johns"].parcel_agricultural_use_codes


def _osceola_ag_where(county: CountyEndpoint) -> str:
    # CAST to integer, not a plain string comparison — confirmed live
    # that a string range on this field silently matches unrelated codes
    # (see module docstring / county_registry notes). CAST(... AS
    # INTEGER) BETWEEN was confirmed to work against this layer's SQL
    # Server backend and returned only genuinely agricultural DORDesc
    # values (14 distinct codes, no false positives) when checked live.
    codes = ",".join(county.parcel_agricultural_use_codes)  # ints as bare literals, no quotes
    return f"CAST({county.parcel_use_code_field} AS INTEGER) IN ({codes})"


def _osceola_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("DORCode")
    if code is None:
        return False
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return False
    valid_ints = {int(c) for c in COUNTIES["osceola"].parcel_agricultural_use_codes}
    return code_int in valid_ints


# Lee's DORCODE is a 2-character string (values '00' through '99', no
# leading zero on the 3-digit statewide DOR_UC form). A 2-char range
# '50'-'69' doesn't hit the same lexicographic false-positive trap that
# 4-char codes have (Osceola/St. Johns) because there are no valid
# 2-char codes with a leading zero that would fall between '50' and '69'
# under string comparison. Live-verified 2026-07-06 via distinct-values
# query + 5-row sample (real DORCODE values '60', '66', '69' all
# returned parcels with LANDUSEDES 'MARKET VALUE AGRICULTURAL').
def _lee_ag_where(county: CountyEndpoint) -> str:
    lo, hi = county.parcel_agricultural_use_code_range
    return f"{county.parcel_use_code_field}>='{lo}' AND {county.parcel_use_code_field}<='{hi}'"


def _lee_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("DORCODE")
    if code is None:
        return False
    lo, hi = COUNTIES["lee"].parcel_agricultural_use_code_range
    return lo <= code <= hi


# Leon's PROP_USE is a 4-character string (real sample: '5400', '5007',
# '6900'). Same lexicographic-trap class as Osceola/St. Johns -- use CAST
# to integer + explicit code list, not a string range. Live-verified
# 2026-07-06: 5-row sample under this WHERE returned real ag parcels
# (POWERHOUSE INC 1616 ac PROP_USE='5400', BRYANT JAMES 7.83 ac
# PROP_USE='6900').
def _leon_ag_where(county: CountyEndpoint) -> str:
    codes = ",".join(county.parcel_agricultural_use_codes)  # ints, no quotes
    return f"CAST({county.parcel_use_code_field} AS INTEGER) IN ({codes})"


def _leon_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("PROP_USE")
    if code is None:
        return False
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return False
    return code_int in {int(c) for c in COUNTIES["leon"].parcel_agricultural_use_codes}


# Citrus's LUC is a 4-character string that also has blank entries (' ')
# and mixed-format values (e.g. '0000', '5000', '6300'). Same CAST-based
# handling as Leon/Osceola. Live-verified 2026-07-06 with 5-row sample
# under this WHERE: real ag parcels returned (LUC '6100' Rich Brandy in
# Crystal River, LUC '5000' Greene Sheila in Lecanto, etc.).
def _citrus_ag_where(county: CountyEndpoint) -> str:
    codes = ",".join(county.parcel_agricultural_use_codes)
    return f"CAST({county.parcel_use_code_field} AS INTEGER) IN ({codes})"


def _citrus_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("LUC")
    if code is None or (isinstance(code, str) and code.strip() == ""):
        return False
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return False
    return code_int in {int(c) for c in COUNTIES["citrus"].parcel_agricultural_use_codes}


# SWFWMD's shared parcel_search MapServer uses a standardized 95-field
# schema across all 16 hosted counties (Wave 2b lead: SWFWMD_URL_ROOT +
# /BaseVector/parcel_search/MapServer/{layer_id}). The `PARUSECODE`
# field is 3-char DOR-style, same range as Pasco -- '050' through '069'
# for agricultural. Live-verified against Sarasota (layer 15) and
# Manatee (layer 10) with real ag parcel samples showing DOR codes
# '050', '060', '062' etc. and PARUSEDESC values like 'ORNAMENTALS,
# MISCELLANEOUS AGRICULTURAL'. One classifier serves every county
# sourced through SWFWMD -- add each county's id to _AG_CLASSIFIERS
# below pointing at this same pair.
def _swfwmd_ag_where(county: CountyEndpoint) -> str:
    lo, hi = county.parcel_agricultural_use_code_range
    return f"{county.parcel_use_code_field}>='{lo}' AND {county.parcel_use_code_field}<='{hi}'"


def _swfwmd_is_agricultural(attrs: dict) -> bool:
    code = attrs.get("PARUSECODE")
    if code is None:
        return False
    # All SWFWMD counties currently use the same '050'-'069' range.
    lo, hi = ("050", "069")
    return lo <= code <= hi


# Registry of per-county (where-clause builder, client-side classifier)
# pairs. Adding a fifth county means adding a new pair here, not editing
# a shared conditional — keeps each county's comparison logic isolated
# and testable on its own.
_AG_CLASSIFIERS: dict[str, tuple[Callable[[CountyEndpoint], str], Callable[[dict], bool]]] = {
    "pasco": (_pasco_ag_where, _pasco_is_agricultural),
    "nassau": (_nassau_ag_where, _nassau_is_agricultural),
    "st_johns": (_st_johns_ag_where, _st_johns_is_agricultural),
    "osceola": (_osceola_ag_where, _osceola_is_agricultural),
    "lee": (_lee_ag_where, _lee_is_agricultural),
    "leon": (_leon_ag_where, _leon_is_agricultural),
    "citrus": (_citrus_ag_where, _citrus_is_agricultural),
    # SWFWMD-sourced counties (Wave 2b, 2026-07-06). All share the same
    # 95-field schema and standardized PARUSECODE ag range -- one
    # classifier pair, multiple registrations.
    "sarasota":  (_swfwmd_ag_where, _swfwmd_is_agricultural),
    "manatee":   (_swfwmd_ag_where, _swfwmd_is_agricultural),
    "hardee":    (_swfwmd_ag_where, _swfwmd_is_agricultural),
    "charlotte": (_swfwmd_ag_where, _swfwmd_is_agricultural),
}


def build_ag_where_clause(county_id: str) -> str:
    county = COUNTIES[county_id]
    where_fn, _ = _AG_CLASSIFIERS[county_id]
    return where_fn(county)


def is_agricultural(county_id: str, attrs: dict) -> bool:
    """Client-side re-check of the server-side WHERE clause's classification for one parcel's attributes."""
    _, classify_fn = _AG_CLASSIFIERS[county_id]
    return classify_fn(attrs)


def _extract_acreage(county: CountyEndpoint, attrs: dict, geometry: Optional[dict]) -> tuple[Optional[float], str]:
    """Returns (acreage, source) — source is "field" or "computed_from_geometry"."""
    if county.parcel_acreage_field is not None:
        raw = attrs.get(county.parcel_acreage_field)
        if raw is not None:
            try:
                return round(float(raw), 2), "field"
            except (TypeError, ValueError):
                pass
    if geometry is not None and geometry.get("rings"):
        return round(polygon_area_acres(geometry["rings"]), 2), "computed_from_geometry"
    return None, "field"


def fetch_candidate_parcels(
    county_id: str,
    min_acreage: float = 20.0,
    max_acreage: float = 4480.0,
    max_candidates: int = 200,
    require_single_owner: bool = False,
    zcta_geometry: Optional[dict] = None,
    skip_parcel_ids: Optional[set[str]] = None,
) -> list[CandidateParcel]:
    """
    Query a county's own parcel layer for parcels that plausibly meet
    the agricultural-use and acreage criteria.

    Fetches geometry directly (single pass) rather than the old
    attrs-then-geometry two-pass split — that split existed specifically
    to avoid pulling full polygon geometry for the statewide layer's
    potentially-thousands-of-matches result set. These county-scoped
    layers are far smaller and confirmed fast even with returnGeometry
    on, and St. Johns needs geometry unconditionally anyway (no acreage
    field), so a single pass is simpler and isn't a real performance
    trade-off here.

    Notes on what this still cannot determine on its own (see also the
    known-gaps list in the project's top-level scan orchestration):
      - "Single owner/entity" across a multi-parcel enclave: only an
        owner-name-string match, not a real ownership/control lookup.
      - 5-year continuous agricultural use: none of these layers carry
        history: this is a single current-year snapshot.
    """
    county = COUNTIES.get(county_id)
    if county is None:
        raise ValueError(f"Unknown county id: {county_id}")
    if county.parcel_service_url is None:
        raise RuntimeError(
            f"No confirmed parcel layer for county '{county_id}' yet — "
            f"see county_registry.py notes."
        )
    if county_id not in _AG_CLASSIFIERS:
        raise RuntimeError(
            f"No agricultural classification function written for county "
            f"'{county_id}' yet. Add one to parcel_fetcher._AG_CLASSIFIERS "
            f"before scanning this county — do not fall back to a generic "
            f"filter, per-county use-code schemes are not interchangeable."
        )

    where = build_ag_where_clause(county_id)

    out_fields_set = {county.parcel_use_code_field, county.parcel_owner_field,
                       county.parcel_owner_field_2, county.parcel_id_field,
                       county.parcel_jurisdiction_field, county.parcel_acreage_field,
                       county.sale_year_field, county.sale_month_field,
                       county.sale_day_field, county.sale_date_field}
    out_fields = ",".join(f for f in out_fields_set if f)

    query_kwargs: dict = dict(
        where=where,
        out_fields=out_fields,
        return_geometry=True,
        out_sr=AREA_SR,
    )
    # ZCTA-scoped fetch: server-side spatial filter cuts the returned set
    # to just parcels intersecting one ZIP-code polygon. Used by the
    # coverage_ledger-driven "advance one ZCTA at a time" flow. The ZCTA
    # geometry is expected to carry its own spatialReference (AREA_SR
    # per zcta_client.get_county_zctas), which query_layer reads to set inSR.
    if zcta_geometry is not None:
        if "spatialReference" not in zcta_geometry:
            zcta_geometry = dict(zcta_geometry, spatialReference={"wkid": AREA_SR})
        query_kwargs["geometry"] = zcta_geometry
        query_kwargs["geometry_type"] = "esriGeometryPolygon"
        query_kwargs["spatial_rel"] = "esriSpatialRelIntersects"

    skip_set: set[str] = skip_parcel_ids or set()

    candidates: list[CandidateParcel] = []
    for feat in query_layer(
        county.parcel_service_url,
        **query_kwargs,
    ):
        attrs = feat.get("attributes", {})
        geometry = feat.get("geometry")

        # Coverage-ledger skip: parcels already fully processed for this
        # county/ZCTA don't need to run through the pipeline again.
        parcel_id_val = attrs.get(county.parcel_id_field) if county.parcel_id_field else None
        if parcel_id_val and parcel_id_val in skip_set:
            continue

        # Defense in depth: re-check the server-side WHERE clause's
        # classification client-side against the actual fetched
        # attributes, rather than trusting the WHERE clause alone.
        if not is_agricultural(county_id, attrs):
            continue

        acreage, acreage_source = _extract_acreage(county, attrs, geometry)
        if acreage is not None and not (min_acreage <= acreage <= max_acreage):
            continue

        candidates.append(CandidateParcel(
            parcel_id=attrs.get(county.parcel_id_field) if county.parcel_id_field else None,
            county_id=county_id,
            acreage=acreage,
            acreage_source=acreage_source,
            use_code=attrs.get(county.parcel_use_code_field),
            use_code_field=county.parcel_use_code_field,
            owner_name=attrs.get(county.parcel_owner_field) if county.parcel_owner_field else None,
            owner_name_2=attrs.get(county.parcel_owner_field_2) if county.parcel_owner_field_2 else None,
            jurisdiction=attrs.get(county.parcel_jurisdiction_field) if county.parcel_jurisdiction_field else None,
            geometry=geometry,
            sold_since_2025=statutory_checks.sold_on_or_after_cutoff(county, attrs),
            single_owner_signal=(
                None if county.parcel_owner_field_2 is None
                else not bool(attrs.get(county.parcel_owner_field_2))
            ),
        ))

        if require_single_owner and candidates[-1].single_owner_signal is False:
            candidates.pop()
            continue

        if len(candidates) >= max_candidates:
            break

    return candidates


def count_matching_candidates(
    county_id: str,
    zcta_geometry: Optional[dict] = None,
    min_acreage: float = 20.0,
    max_acreage: float = 4480.0,
    require_single_owner: bool = False,
) -> int:
    """
    Count parcels that `fetch_candidate_parcels` WOULD return with the
    given filters -- unbounded (no `max_candidates` cap) and no
    `skip_parcel_ids`. Deliberately shares the exact same code path as
    the fetcher, so the ledger's `total_candidates` and the fetcher's
    result set can never silently diverge.

    Roadmap item 11 (2026-07-06): the old ledger stored a
    total_candidates computed by `zcta_client.count_parcels_in_zcta`,
    which only applied the server-side ag WHERE clause + spatial
    intersect. `fetch_candidate_parcels` then adds client-side filters
    (min/max acreage, `is_agricultural` re-check, single-owner). Any
    parcel that matched the WHERE but failed a client-side filter
    inflated total_candidates without ever being fetch-able -- so the
    ledger's "N remaining" number never reached zero, and background
    jobs terminated with "0 rows but N candidates remaining." Using
    this function instead of the bare server count closes that gap.

    This is more expensive than a `returnCountOnly=true` query (pulls
    attributes and geometry for every match, not just a count), but
    the total is stored in the ledger and computed at most once per
    ZCTA per process, so the extra cost is one-time.
    """
    return len(fetch_candidate_parcels(
        county_id=county_id,
        min_acreage=min_acreage,
        max_acreage=max_acreage,
        max_candidates=10**9,  # effectively unbounded
        zcta_geometry=zcta_geometry,
        require_single_owner=require_single_owner,
        skip_parcel_ids=None,
    ))


def group_by_apparent_owner(parcels: list[CandidateParcel]) -> dict[str, list[CandidateParcel]]:
    """
    Best-effort grouping of parcels that share an exact owner name
    string, as a starting point for identifying multi-parcel enclaves
    under common control. NOT a substitute for a title search: LLCs,
    trusts, and family entities frequently hold contiguous land under
    slightly different name variants, which this naive grouping will miss.
    """
    grouped: dict[str, list[CandidateParcel]] = {}
    for p in parcels:
        key = (p.owner_name or "UNKNOWN").strip().upper()
        grouped.setdefault(key, []).append(p)
    return grouped
