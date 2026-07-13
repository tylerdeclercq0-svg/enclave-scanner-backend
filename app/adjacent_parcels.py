"""
Adjacent-parcel fetching and built-status classification for the false-
positive fix (2026-07-13).

Motivation: `encirclement.compute_encirclement` reads FLUM (future-land-
use) designations, not built status. A 640-acre cow pasture bordered by
a residential FLUM overlay scores 100% "qualifying" perimeter today,
even though the neighbor is actually undeveloped rural land. SB 686
s. 163.3164(4)(c)1.a requires the parcel to be "surrounded on at least
75 percent of their perimeter by [existing] industrial, commercial, or
residential development" -- an existing-development test, not a FLUM
test. This module adds the missing built-status signal.

Design:
- fetch_adjacent_parcels() spatially queries the county's OWN parcel
  layer (not FLUM) for every parcel intersecting a small buffer around
  the candidate. Reuses each CountyEndpoint's parcel_service_url +
  parcel_use_code_field + parcel_acreage_field + parcel_owner_field
  wiring already ground-truthed for all 13 confirmed_live counties.
- is_built_parcel() normalizes each county's use-code encoding to a
  DOR class integer and checks it against the DOR built-class set
  (residential 1-8 / commercial 10-19 / industrial 20-27 / institutional
  71-89). Handles both 2-3 char DOR-standard encodings (Pasco, Nassau,
  Lee, SWFWMD-shared counties) and 4-char county-local encodings
  packing DOR class * 100 + subcategory (St. Johns, Osceola, Citrus,
  Leon).
- compute_option1_pct() measures how much of the candidate's perimeter
  falls inside actually-built adjacent parcels. Same boundary-in-area
  math as encirclement.compute_encirclement, with the same 150-ft
  ROW/canal substitution.
- compute_surrounding_density() returns urban/suburban/rural/unknown
  based on built-fraction + average adjacent acreage. Replaces the
  FLUM-string keyword classifier in flu_taxonomy.classify_density()
  which was returning "unknown" for most Pasco/Manatee/Hardee parcels
  because their FLUM abbreviations don't hit descriptive keywords.
- detect_self_surrounding() flags institutional landholder patterns
  where an owner appears on multiple adjacent parcels -- an ag parcel
  cannot self-qualify for Option 1.

year_built is deliberately NOT checked -- no county's parcel layer
exposes it in our fetcher today. The DOR use-code check catches
residential/commercial/industrial parcels regardless; we lose the
edge case of an ag-coded lot with a house on it, documented on
is_built_parcel().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from arcgis_client import query_layer
from county_registry import CountyEndpoint
import service_windows


# Florida DOR standard use-code classes indicating actual existing
# development, per the DOR NAL Users Guide (floridarevenue.com):
# - 01-08: single-family / mobile home / multi-family / condo / co-op /
#          retirement / miscellaneous residential.
# - 10-19: vacant commercial (10) + retail / office / hotel / restaurant /
#          service / financial / repair / recreational commercial.
# - 20-27: heavy industrial / manufacturing / mineral proc / warehousing /
#          lumber yard / packing plant / cannery / distillery.
# - 71-89: institutional / government / religious / educational /
#          hospital / cultural / military / municipal (30-39 are ag-
#          adjacent utilities/parks that we deliberately EXCLUDE, since
#          those are typically not "development" in the enclave-statute
#          sense; 40-49 utilities also excluded; 90-99 are non-taxable
#          state/federal/river land, excluded).
#
# DOR class 10 ("Vacant Commercial") is included even though "vacant"
# sounds unbuilt: in DOR terminology it means zoned/coded commercial in
# a suburban context, satisfying the statute's category test. Same for
# DOR 40 ("Vacant Industrial") which we DO NOT include here -- keeping
# the built-status test conservative on the industrial side because
# "vacant industrial" more commonly means genuinely undeveloped
# industrial-zoned rural land in Florida county records.
BUILT_DOR_CLASSES = frozenset(range(1, 28)) | frozenset(range(71, 90))

# How far to reach outward from the candidate's boundary when fetching
# adjacent parcels. Matches encirclement.py's ADJACENCY_BUFFER_FEET so
# both spatial queries capture the same neighbors (edge-touching under
# floating-point noise) without silently truncating some.
ADJACENT_PARCEL_BUFFER_FEET = 50.0

# Self-surrounding thresholds. An owner appearing on >=MIN_MATCHES
# adjacent parcels AND owning at least MIN_TOTAL_ACRES of those same-
# owner adjacents together flags institutional-landholder risk. Kept
# as module-level constants so the backfill and live scan use the
# same values.
#
# The acreage AND-gate was added 2026-07-13 after the initial three-
# parcel verification correctly flagged Farmland Reserve Inc (8/8
# adjacents, all owned by Farmland Reserve, huge Osceola timberland
# tracts easily >1,000 ac combined) but OVER-flagged Mathis Edward
# Neil (3/21 adjacents share the name -- a legitimate individual
# landowner with a few contiguous small holdings, not an institutional
# landholder). Requiring MIN_TOTAL_ACRES separates the two: individual
# owners rarely control 500+ ac of contiguous adjacent parcels;
# institutional ranchers/timber/religious/development entities routinely
# do.
SELF_SURROUNDING_MIN_MATCHES = 3
SELF_SURROUNDING_MIN_TOTAL_ACRES = 500.0


@dataclass
class AdjacentParcel:
    parcel_id: Optional[str]
    use_code: Optional[str]
    acres: Optional[float]
    owner_name: Optional[str]
    geometry: Optional[dict]  # Esri rings, in AREA_SR (3086)


def _dor_class_from_use_code(use_code: Optional[str]) -> Optional[int]:
    """
    Normalize a raw parcel use-code string to a standard DOR class
    integer (00-99). Two encoding families in the 13-county set:

    - 2-3 character codes: the raw int IS the DOR class already.
      Pasco DIR_CLASS ('054'), Nassau DORUC ('055'), Lee DORCODE ('50'),
      SWFWMD-shared PARUSECODE ('069').

    - 4-character codes: DOR class * 100 + subcategory. St. Johns
      USE_CODE ('0100' = DOR 01 SFR, '5300' = DOR 53 crop), Osceola
      DORCode ('5101' = DOR 51 improved cropland), Citrus LUC ('5000' =
      DOR 50), Leon PROP_USE ('5007' = DOR 50 subcat 07). Divide by 100
      to recover the DOR class.

    None for unparseable / blank values so is_built_parcel can fall
    back to a safe "not built" default rather than treating unknowns
    as built (which would repeat the same over-permissive bug the
    FLUM-only path had).
    """
    if not use_code:
        return None
    try:
        n = int(str(use_code).strip())
    except (TypeError, ValueError):
        return None
    return n // 100 if n >= 1000 else n


def is_built_parcel(adj: AdjacentParcel) -> bool:
    """
    True iff the adjacent parcel's DOR use-code classifies as existing
    development. See BUILT_DOR_CLASSES for the exact set and rationale.

    year_built is deliberately not checked -- no county's parcel layer
    exposes it in our fetcher today (per the 2026-07-13 architectural
    review). The DOR use-code check catches genuine residential /
    commercial / industrial / institutional parcels regardless; we lose
    the edge case of an ag-coded lot with a house on it, but that's
    rare -- usually those get recoded to DOR class 01 (SFR) by the
    property appraiser once the structure is on record.
    """
    dor = _dor_class_from_use_code(adj.use_code)
    if dor is None:
        return False
    return dor in BUILT_DOR_CLASSES


def _adjacent_parcels_where(county: CountyEndpoint, exclude_parcel_id: Optional[str]) -> str:
    """
    WHERE clause for the adjacent-parcel query. Includes any parcel of
    ANY use code (unlike parcel_fetcher's ag-only WHERE), minus the
    candidate parcel's own ID. Nassau's shared Baker/Nassau layer keeps
    the CNTYNAME='NASSAU' scoping fragment.
    """
    parts: list[str] = []
    if county.parcel_county_filter:
        parts.append(county.parcel_county_filter)
    if exclude_parcel_id and county.parcel_id_field:
        # ArcGIS SQL string escaping: double any embedded single quotes.
        escaped = str(exclude_parcel_id).replace("'", "''")
        parts.append(f"{county.parcel_id_field} <> '{escaped}'")
    return " AND ".join(parts) if parts else "1=1"


def _read_acres(county: CountyEndpoint, attrs: dict, geometry: Optional[dict]) -> Optional[float]:
    """
    Adjacent-parcel acreage. Prefers the county's acreage_field where
    present; falls back to geometry-computed area (same helper as
    parcel_fetcher, no need to re-derive here) for counties like St.
    Johns and Citrus that don't populate an acreage field.
    """
    if county.parcel_acreage_field:
        raw = attrs.get(county.parcel_acreage_field)
        if raw is not None:
            try:
                return round(float(raw), 2)
            except (TypeError, ValueError):
                pass
    if geometry and geometry.get("rings"):
        from parcel_fetcher import polygon_area_acres
        try:
            return round(polygon_area_acres(geometry["rings"]), 2)
        except Exception:  # noqa: BLE001 -- degenerate geometry, treat as unknown
            return None
    return None


def fetch_adjacent_parcels(
    county: CountyEndpoint,
    candidate_geometry: dict,
    candidate_parcel_id: Optional[str],
    buffered_geometry: Optional[dict] = None,
) -> list[AdjacentParcel]:
    """
    Spatially query the county's own parcel layer for every parcel
    intersecting the candidate's buffered boundary, excluding the
    candidate itself. Returns the raw list -- callers filter to built
    parcels via is_built_parcel() for the Option 1 calculation, but
    keep the full list for surrounding_density and self-surrounding
    detection which need every neighbor regardless of use code.

    `buffered_geometry` lets a caller reuse the already-buffered
    geometry from the FLUM neighbor fetch (scan_orchestrator builds one
    per parcel via _buffer_esri_geometry) instead of buffering again.
    When None, this function buffers using ADJACENT_PARCEL_BUFFER_FEET
    on the fly -- used by the backfill script, which doesn't have the
    scan orchestrator's buffered_geom in hand.

    Raises RuntimeError with the standard "outside service window"
    message when called against a SWFWMD-sourced county outside
    6 AM-10 PM Eastern -- callers upstream of a batch job are expected
    to have already checked this, but the guard is here so an ad-hoc
    call (e.g. from the backfill script) fails fast rather than
    silently returning a broken result.
    """
    if county.parcel_service_url is None:
        return []

    if not service_windows.parcel_source_within_window(county.parcel_source):
        raise RuntimeError(
            service_windows.parcel_source_window_message(county.parcel_source)
        )

    if buffered_geometry is None:
        # Late import to avoid the circular scan_orchestrator <-> here
        # dependency at module load time.
        from scan_orchestrator import _buffer_esri_geometry
        buffered_geometry = _buffer_esri_geometry(
            candidate_geometry, ADJACENT_PARCEL_BUFFER_FEET
        )

    # Pull every field we might need. Some may be None on this county
    # (e.g. Nassau has no owner_2), which is fine -- the set-comprehension
    # drops None entries and query_layer accepts a comma-joined subset.
    fields: set[str] = {
        f for f in (
            county.parcel_id_field,
            county.parcel_use_code_field,
            county.parcel_owner_field,
            county.parcel_owner_field_2,
            county.parcel_acreage_field,
        ) if f
    }
    out_fields = ",".join(fields) if fields else None

    where = _adjacent_parcels_where(county, candidate_parcel_id)

    from parcel_fetcher import AREA_SR  # avoids a hard import cycle at module load

    query_kwargs: dict = dict(
        where=where,
        out_fields=out_fields,
        return_geometry=True,
        out_sr=AREA_SR,
        geometry=buffered_geometry,
        geometry_type="esriGeometryPolygon",
        spatial_rel="esriSpatialRelIntersects",
        # Stable pagination -- required for SWFWMD's parcel_search
        # MapServer (no OBJECTID field surfaced), no-op cost elsewhere.
        order_by=(f"{county.parcel_id_field} ASC" if county.parcel_id_field else None),
    )

    adjacents: list[AdjacentParcel] = []
    seen: set[str] = set()
    for feat in query_layer(county.parcel_service_url, **query_kwargs):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry")
        pid = attrs.get(county.parcel_id_field) if county.parcel_id_field else None
        # Item-14-style dedup (see parcel_fetcher.fetch_candidate_parcels
        # for the full story on why this matters).
        if pid:
            if pid in seen:
                continue
            seen.add(pid)
            # Second-line defense against the ExtractParcelID case where
            # the county DB returns the candidate parcel itself despite
            # the WHERE-clause exclusion. Some ArcGIS servers ignore
            # inequality on non-indexed fields.
            if candidate_parcel_id and str(pid) == str(candidate_parcel_id):
                continue

        owner = attrs.get(county.parcel_owner_field) if county.parcel_owner_field else None
        adjacents.append(AdjacentParcel(
            parcel_id=str(pid) if pid is not None else None,
            use_code=(str(attrs.get(county.parcel_use_code_field))
                      if county.parcel_use_code_field
                      and attrs.get(county.parcel_use_code_field) is not None
                      else None),
            acres=_read_acres(county, attrs, geom),
            owner_name=str(owner) if owner is not None else None,
            geometry=geom,
        ))
    return adjacents


def compute_option1_pct(
    candidate_geometry: dict,
    adjacent_parcels: list[AdjacentParcel],
    row_substitution_feet: float = 150.0,
) -> tuple[float, int]:
    """
    Fraction of the candidate's perimeter (0-100) that falls inside
    actually-built adjacent parcels, per s. 163.3164(4)(c)1.a. Same
    boundary-line-inside-area math as encirclement.compute_encirclement,
    just against parcel polygons filtered by is_built_parcel() instead
    of FLUM polygons filtered by classify_flu_value().

    Returns (option1_pct, built_neighbor_count). built_neighbor_count
    is surfaced on the row for the frontend's "N of M adjacent parcels
    are built" hover, and for the backfill audit.

    The 150-ft ROW/canal substitution matches encirclement.py's default
    (typical local/collector ROW 60-120 ft, arterials 100-150 ft,
    Florida residential canals 60-100 ft) so a real built subdivision
    across a road counts toward Option 1 the same way its FLUM
    designation would.
    """
    from encirclement import esri_json_to_shapely  # local import breaks scan-time cycle

    if not adjacent_parcels:
        return 0.0, 0

    try:
        candidate_poly = esri_json_to_shapely(candidate_geometry)
        boundary = candidate_poly.boundary
        total_perimeter = boundary.length
    except Exception:  # noqa: BLE001 -- degenerate candidate geometry
        return 0.0, 0
    if total_perimeter <= 0:
        return 0.0, 0

    row_substitution_meters = max(0.0, row_substitution_feet) * 0.3048

    built_length = 0.0
    built_count = 0
    for adj in adjacent_parcels:
        if not is_built_parcel(adj) or adj.geometry is None:
            continue
        try:
            neighbor_poly = esri_json_to_shapely(adj.geometry)
        except Exception:  # noqa: BLE001
            continue

        shared = boundary.intersection(neighbor_poly)
        shared_length = shared.length
        if row_substitution_meters > 0:
            buffered_shared = boundary.intersection(
                neighbor_poly.buffer(row_substitution_meters)
            )
            shared_length = max(shared_length, buffered_shared.length)

        if shared_length <= 0:
            continue
        built_length += shared_length
        built_count += 1

    # Defensive cap: same reasoning as encirclement.compute_encirclement.
    built_length = min(built_length, total_perimeter)
    pct = (built_length / total_perimeter * 100.0) if total_perimeter > 0 else 0.0
    return round(pct, 1), built_count


def compute_surrounding_density(adjacent_parcels: list[AdjacentParcel]) -> str:
    """
    Derive an urban/suburban/rural/unknown density label from the
    adjacent-parcel population. Replaces flu_taxonomy.classify_density
    which reads dominant FLUM string via keyword match -- that returned
    "unknown" for most Pasco/Manatee/Hardee parcels because their FLUM
    abbreviations (RES-6, PD, AG-R) don't hit descriptive keywords, so
    the frontend's density facet was ~empty in production.

    Two dimensions combined per the 2026-07-13 fix spec:
    - built_fraction: what share of adjacent parcels are built (Option
      1's signal, but summarized as a ratio instead of a perimeter %).
    - avg_adjacent_acres: how big the neighbors are on average --
      2 ac in urban, 10 ac suburban, 50 ac rural is a rough working
      map to Florida's land-use pattern.
    """
    if not adjacent_parcels:
        return "unknown"

    total = len(adjacent_parcels)
    built = sum(1 for p in adjacent_parcels if is_built_parcel(p))
    built_fraction = built / total

    acre_values = [p.acres for p in adjacent_parcels if p.acres is not None]
    avg_acres = sum(acre_values) / len(acre_values) if acre_values else None

    if built_fraction >= 0.60 or (avg_acres is not None and avg_acres < 2):
        return "urban"
    if built_fraction >= 0.30 or (avg_acres is not None and avg_acres < 10):
        return "suburban"
    if built_fraction >= 0.10 or (avg_acres is not None and avg_acres < 50):
        return "rural"
    # Deep-rural (avg adjacent parcel 50+ acres of ag/timberland with
    # zero built parcels) is left as "unknown" rather than mislabelling
    # it "rural" -- the frontend's density facet distinguishes them.
    return "unknown"


def detect_self_surrounding(
    subject_owner: Optional[str],
    adjacent_parcels: list[AdjacentParcel],
    min_matches: int = SELF_SURROUNDING_MIN_MATCHES,
    min_total_acres: float = SELF_SURROUNDING_MIN_TOTAL_ACRES,
) -> bool:
    """
    True if the candidate's own owner name appears on `min_matches`
    or more adjacent parcels AND those same-owner adjacents together
    exceed `min_total_acres`. Both gates must fire.

    Purpose: guard against institutional landholders (Farmland Reserve
    Inc / Deseret Ranch, Walt Disney Parks and Resorts / R.C.I.D.,
    Rayonier timberland tracts, etc.) whose adjacent parcels are their
    own other holdings -- you cannot self-qualify for Option 1's
    built-encirclement test. The scan orchestrator caps option1_pct
    at 0 whenever this returns True.

    Why the acreage AND-gate: the count-only rule over-flagged
    legitimate individual landowners with a few small adjacent
    holdings (Mathis Edward Neil in Pasco: 3/21 adjacent parcels
    shared the name, but they were small parcels totaling far under
    500 ac -- not the institutional pattern this flag exists to
    catch). Institutional landholders effectively always control 500+
    ac of contiguous same-owner land; ordinary individual landowners
    almost never do.

    Case-insensitive, whitespace-trimmed exact match on owner_name.
    Adjacent parcels with acres=None (rare: degenerate geometry that
    also has no county acreage_field) contribute 0 to the acres
    total -- documented, not a bug. An owner-string similarity check
    (fuzzy match on "SMITH FAMILY LLC" vs "SMITH FAMILY REVOCABLE
    TRUST") would catch related-entity ownership too, but is
    deliberately out of scope here -- explicit exact match is easier
    to explain in a diligence report and doesn't silently flag
    unrelated same-family owners.
    """
    if not subject_owner:
        return False
    owner_upper = subject_owner.upper().strip()
    if not owner_upper:
        return False

    match_count = 0
    total_acres = 0.0
    for p in adjacent_parcels:
        if not p.owner_name:
            continue
        if p.owner_name.upper().strip() == owner_upper:
            match_count += 1
            if p.acres is not None:
                total_acres += p.acres
    return match_count >= min_matches and total_acres >= min_total_acres
