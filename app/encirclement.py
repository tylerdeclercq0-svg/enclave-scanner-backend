"""
Encirclement / perimeter analysis — the actual geometric core of the
agricultural enclave screen.

SB 686 (s. 163.3164(4)(c), F.S.) requires a parcel to be surrounded on
at least a threshold percentage of its perimeter by qualifying
neighboring land (existing development, FLUM-designated development,
or an interstate/USB combination, depending on which of the five
pathways is being tested). That is a real GIS operation: buffer the
candidate parcel's boundary, find what FLUM polygons it touches, and
measure what fraction of the perimeter length is shared with
qualifying neighbors versus non-qualifying ones (or gaps/right-of-way).

This module uses Shapely for the geometry math. It is written for an
environment with Shapely installed and network access to fetch FLUM
polygons — neither is available in this sandbox, so nothing here has
been run against live data. The math itself (buffer, intersection,
length ratio) is standard and does not depend on network access once
the input geometries are in hand.

Requires: shapely >= 2.0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    from shapely.geometry import shape, Polygon, MultiPolygon
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover
    shape = Polygon = MultiPolygon = unary_union = None  # type: ignore


# How far outside the candidate parcel's boundary to look for neighbors.
# A small positive buffer (in the layer's native units — typically feet
# for Florida State Plane projections) avoids missing adjacent parcels
# that share a boundary but don't technically overlap due to floating-
# point precision in the source data.
ADJACENCY_BUFFER_FEET = 5.0

# Right-of-way / canal handling: per s. 163.3164(4), F.S., "where a
# right-of-way, body of water, or canal exists along the perimeter of a
# parcel, the perimeter calculations of the agricultural enclave must be
# based on the adjacent parcel or parcels across the right-of-way, body
# of water, or canal." Implemented 2026-07-06 via ROW_SUBSTITUTION_FEET
# below and the buffered-neighbor intersection in compute_encirclement().
# Typical local/collector road ROW is 60-120 ft, arterials 100-150 ft,
# residential canals in Florida are typically 60-100 ft wide. 150 ft is
# a conservative default that reaches across all three cases without
# routinely engulfing non-adjacent second-row parcels.
ROW_SUBSTITUTION_FEET = 150.0


@dataclass
class PerimeterSegment:
    length: float
    flu_value: Optional[str]
    is_qualifying: bool
    neighbor_parcel_acreage: Optional[float] = None


@dataclass
class EncirclementResult:
    total_perimeter: float
    qualifying_perimeter: float
    pct_qualifying: float
    segments: list[PerimeterSegment]
    candidate_pathways: list[int]


def _require_shapely():
    if shape is None:
        raise ImportError(
            "shapely is required for encirclement analysis. "
            "Install with: pip install shapely"
        )


def _signed_ring_area(ring: list[list[float]]) -> float:
    """
    Shoelace formula, signed (not absolute). Same convention as
    parcel_fetcher.polygon_area_acres/_signed_ring_area (kept as a
    separate copy here rather than importing across modules for a
    four-line helper) -- sign encodes winding direction, which is how
    Esri's REST API actually distinguishes exterior rings from holes,
    NOT ring order in the array.
    """
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def esri_json_to_shapely(esri_geom: dict):
    """
    Convert an ArcGIS REST 'rings' polygon geometry to a Shapely
    Polygon/MultiPolygon.

    FIXED 2026-07-03: the previous version assumed the first ring is
    always the sole exterior and every other ring is a hole. Confirmed
    live against Pasco's FLUM layer that this is false — real FLUM
    polygons are genuinely multipart (multiple disjoint exterior rings
    in one geometry, e.g. a land-use designation covering two separate
    tracts). Naively passing all non-first rings to Shapely's
    Polygon(exterior, holes=...) produced an INVALID, self-intersecting
    polygon whose `.intersection()` with the real candidate parcel
    silently returned nonsense (confirmed live: intersection area came
    back equal to the candidate's own full area, and boundary-to-
    boundary intersection length came back 0 despite real overlap) —
    this was the root cause of every candidate showing 0% qualifying
    perimeter during the first live end-to-end pipeline run, not a
    genuine "no qualifying neighbors" result.

    Fix: classify each ring as exterior vs. hole by winding direction
    (Esri's REST API convention: clockwise = exterior, counter-
    clockwise = hole — a NEGATIVE signed shoelace sum here means
    clockwise), then assign each hole to whichever exterior ring
    actually contains it, rather than assuming a fixed ring order.
    """
    _require_shapely()
    rings = esri_geom.get("rings", [])
    if not rings:
        raise ValueError("Geometry has no rings")

    exterior_rings = [r for r in rings if _signed_ring_area(r) < 0]
    hole_rings = [r for r in rings if _signed_ring_area(r) >= 0]

    if not exterior_rings:
        # Malformed/degenerate data (e.g. a single ring that happens to
        # wind counter-clockwise) — fall back to treating the first ring
        # as the sole exterior rather than producing an empty geometry.
        exterior_rings = [rings[0]]
        hole_rings = [r for r in rings[1:] if r is not rings[0]]

    parts = []
    for ext in exterior_rings:
        ext_poly = Polygon(ext)
        my_holes = [h for h in hole_rings if ext_poly.contains(Polygon(h).representative_point())]
        parts.append(Polygon(ext, my_holes) if my_holes else ext_poly)

    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


def get_centroid_lat_lon(esri_geom: dict, source_wkid: int = 3086) -> Optional[tuple[float, float]]:
    """
    Extract a parcel's centroid as (lat, lon) in WGS84, for map display
    in the frontend. The statewide cadastral layer's native spatial
    reference is WKID 3086 (Florida Albers, in meters).

    CHANGED: previously used pyproj for this conversion, but pyproj was
    confirmed to fail to initialize on Render ("TypeError: expected
    bytes, str found" from Transformer.from_crs — a known category of
    pyproj/PROJ deployment fragility). Replaced with the same
    hand-written Albers Equal-Area Conic formula (inverse direction)
    used in parcel_fetcher.py's boundary reprojection, verified against
    a known real-world point (Tampa, FL) during live testing. Only
    supports source_wkid=3086 (the statewide layer's actual SR) — any
    other value raises, since the hand-written formula is specific to
    this one projection's parameters, unlike pyproj which would have
    handled arbitrary CRS pairs.
    """
    _require_shapely()
    if source_wkid != 3086:
        raise ValueError(
            f"get_centroid_lat_lon only supports source_wkid=3086 "
            f"(Florida Albers) with the current hand-written projection "
            f"formula — got {source_wkid}. Extend "
            f"_florida_albers_to_latlon if another source SR is needed."
        )

    try:
        poly = esri_json_to_shapely(esri_geom)
        centroid = poly.centroid
        x, y = centroid.x, centroid.y
    except (ValueError, TypeError, AttributeError):
        return None

    lon, lat = _florida_albers_to_latlon(x, y)
    return (lat, lon)


def _florida_albers_to_latlon(x: float, y: float) -> tuple[float, float]:
    """
    Inverse Albers Equal-Area Conic projection: converts Florida
    Albers (WKID 3086) coordinates back to WGS84 lon/lat. Uses the same
    published projection parameters as the forward formula in
    parcel_fetcher.py's _reproject_latlon_geometry_to_florida_albers
    (central meridian -84.0°, standard parallels 24.0°/31.5°, GRS 1980
    ellipsoid) — verified together against a known real-world point
    (Tampa, FL) during live testing, confirming both directions agree.
    """
    import math

    a = 6378137.0
    f = 1 / 298.257222101
    e2 = 2 * f - f * f
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

    x_adj = x - false_easting
    y_adj = rho0 - (y - false_northing)
    rho = math.sqrt(x_adj * x_adj + y_adj * y_adj)
    theta = math.atan2(x_adj, y_adj)

    q_val = (c - (rho * n / a) ** 2) / n
    lat = math.asin(max(-1.0, min(1.0, q_val / (1 - (1 - e2) / (2 * e) * math.log((1 - e) / (1 + e))))))
    # Iterative refinement for accuracy (standard Albers inverse formula)
    for _ in range(5):
        sin_lat = math.sin(lat)
        lat = lat + ((1 - e2 * sin_lat * sin_lat) ** 2 / (2 * math.cos(lat))) * (
            q_val / (1 - e2) - sin_lat / (1 - e2 * sin_lat * sin_lat)
            + (1 / (2 * e)) * math.log((1 - e * sin_lat) / (1 + e * sin_lat))
        )
    lon = lon0 + theta / n

    return math.degrees(lon), math.degrees(lat)


def classify_flu_value(flu_value: str, agricultural_values: tuple[str, ...]) -> str:
    """
    Bucket a county's raw FLUM category string into one of the buckets
    the statute cares about. This is necessarily county-specific —
    every county names its categories differently (compare Hillsborough's
    'SMU-6 SUBURBAN MIXED USE' to Orange's 'Mixed Use Corridor') — so
    this function takes a per-county keyword list rather than a single
    hardcoded mapping. The keyword lists below are a starting point and
    should be reviewed against each county's actual FLUM legend before
    trusting the classification at scale.
    """
    v = (flu_value or "").upper()

    if any(v.startswith(a.upper()) for a in agricultural_values):
        return "agricultural"

    industrial_kw = ("INDUSTRIAL", "EMPLOYMENT", "WAREHOUSE", "HEAVY", "LIGHT INDUSTRIAL")
    commercial_kw = ("COMMERCIAL", "OFFICE", "RETAIL", "MIXED USE", "CORRIDOR", "MU-")
    residential_kw = ("RESIDENTIAL", "RES-", "RESIDENTIAL PLANNED", "URBAN LOW", "URBAN MEDIUM")

    if any(k in v for k in industrial_kw):
        return "industrial"
    if any(k in v for k in commercial_kw):
        return "commercial"
    if any(k in v for k in residential_kw):
        return "residential"
    return "other"


def compute_encirclement(
    candidate_geometry: dict,
    neighbor_features: list[dict],
    flu_field: str,
    agricultural_flu_values: tuple[str, ...],
    row_substitution_feet: float = ROW_SUBSTITUTION_FEET,
) -> EncirclementResult:
    """
    Core perimeter-adjacency calculation.

    candidate_geometry: the candidate parcel's ArcGIS-format geometry.
    neighbor_features: FLUM features returned from a spatial query
        against the candidate's buffered boundary (see
        arcgis_client.query_layer with geometry= and spatial_rel=
        "esriSpatialRelIntersects").
    flu_field: the field name on the FLUM layer holding the land use
        code/label, per CountyEndpoint.flu_field.
    agricultural_flu_values: the values on this county's FLUM layer that
        represent agricultural/rural use — used to identify which
        adjoining polygons do NOT count as qualifying development.

    Returns the fraction of the candidate's perimeter that touches
    qualifying (industrial/commercial/residential, per the statute)
    neighbor polygons, broken out by segment so the result can be
    inspected and audited rather than trusted as a single black-box
    number.
    """
    _require_shapely()

    candidate_poly = esri_json_to_shapely(candidate_geometry)
    boundary = candidate_poly.boundary
    total_perimeter = boundary.length

    row_substitution_meters = max(0.0, row_substitution_feet) * 0.3048

    segments: list[PerimeterSegment] = []
    qualifying_perimeter = 0.0

    for feat in neighbor_features:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry")
        if geom is None:
            continue
        try:
            neighbor_poly = esri_json_to_shapely(geom)
        except (ValueError, TypeError):
            continue

        # FIXED 2026-07-03: was `boundary.intersection(neighbor_poly.boundary)`
        # -- a boundary-to-boundary line intersection, which requires the
        # candidate's edge to exactly coincide with the FLUM polygon's
        # edge. Confirmed live against a real Pasco parcel that this is
        # almost never true: FLUM designations are a land-use overlay,
        # not a parcel-boundary layer, and routinely CONTAIN a candidate
        # parcel entirely (future land use can already cover ground the
        # parcel sits on, even though the parcel's current use is still
        # agricultural) rather than merely bordering it. The old check
        # returned a real neighbor polygon with a real qualifying FLU
        # code but 0.0 shared length every time, silently reporting "0%
        # encircled" for every candidate regardless of its real
        # surroundings. Measuring how much of the candidate's boundary
        # LINE falls inside the neighbor's AREA (not its boundary) is
        # the correct check and handles both cases: a parcel sitting
        # inside one big qualifying FLUM zone (full perimeter counts),
        # and a parcel merely bordering a smaller adjacent zone (only
        # the touching stretch counts).
        shared = boundary.intersection(neighbor_poly)
        shared_length = shared.length

        # ROW/canal/water-body substitution rule (s. 163.3164(4), F.S.):
        # if a right-of-way runs between the candidate and this neighbor,
        # the direct check above returns ~0 for that stretch, silently
        # under-counting qualifying perimeter. Buffering the neighbor
        # outward by a typical ROW width lets it "reach across" the gap.
        # We use max() rather than adding: if the neighbor is already
        # directly adjacent, the direct length is what we want (buffering
        # inflates it slightly at corners); if there IS a gap, the
        # buffered version is strictly larger and correctly credits the
        # far-side neighbor for the ROW-blocked stretch. The existing
        # >=100% cap below protects against two neighbors both reaching
        # into the same gap and double-counting.
        if row_substitution_meters > 0:
            buffered_shared = boundary.intersection(
                neighbor_poly.buffer(row_substitution_meters)
            )
            shared_length = max(shared_length, buffered_shared.length)

        if shared_length <= 0:
            continue

        flu_value = attrs.get(flu_field)
        bucket = classify_flu_value(flu_value, agricultural_flu_values)
        is_qualifying = bucket in ("industrial", "commercial", "residential")

        segments.append(PerimeterSegment(
            length=shared_length,
            flu_value=flu_value,
            is_qualifying=is_qualifying,
        ))
        if is_qualifying:
            qualifying_perimeter += shared_length

    # Defensive cap: FLUM layers are expected to be a clean planar
    # partition (no gaps/overlaps), so summed segment lengths should
    # never exceed the real perimeter, but don't let a real-world data
    # overlap produce a nonsensical >100% result.
    qualifying_perimeter = min(qualifying_perimeter, total_perimeter)
    pct_qualifying = (qualifying_perimeter / total_perimeter * 100) if total_perimeter > 0 else 0.0

    # Pathway determination is intentionally left to a separate function
    # (determine_pathways) since it also needs acreage and USB/interstate
    # adjacency, which this function does not have.
    return EncirclementResult(
        total_perimeter=total_perimeter,
        qualifying_perimeter=qualifying_perimeter,
        pct_qualifying=round(pct_qualifying, 1),
        segments=segments,
        candidate_pathways=[],
    )


def determine_pathways(
    encirclement: EncirclementResult,
    acreage: float,
    adjacent_to_interstate: bool,
    adjacent_to_usb: bool,
    designated_pct_existing_development: Optional[float] = None,
    inside_rural_study_area: bool = False,
    interstate_frontage_pct: float = 0.0,
    usb_perimeter_pct: float = 0.0,
) -> list[int]:
    """
    Map an encirclement result plus a few other facts onto the statutory
    pathways in s. 163.3164(4)(c), F.S. The enrolled bill text (verified
    2026-07-06 against flsenate.gov/Session/Bill/2026/686/BillText/er,
    Ch. 2026-34) organizes (c) as one preamble ("Are surrounded on at
    least 75 percent of their perimeter by:") followed by (c)1 with three
    OR-separated sub-alternatives (a/b/c), then (c)2, then (c)3. This
    project labels them Options 1-5 for continuity with prior code, but
    the statute-to-Option mapping is:

      Option 1 = (c)1.a: existing industrial/commercial/residential
        development (the 75% comes from the (c) preamble).
      Option 2 = (c)1.b: FLUM-designated for such development AND >=75%
        of those designated parcels already have existing development.
        NOTE: the 75% figure here was corrected from an earlier draft
        that used 50% — the enrolled bill text requires 75%, not 50%.
      Option 3 = (c)1.c: combination of an interstate highway AND
        parcels within an urban service district/area/line that are
        designated for development.
      Option 4 = (c)2: parcel(s) <=700 acres, with the perimeter split
        between designated-development parcels (>=50%) AND parcels
        within a USB (>=50%) — these can be the same or different
        segments.
      Option 5 = (c)3: located within an established rural study area
        adopted in the local government's comprehensive plan which was
        intended to be developed with residential uses. This is a pure
        boundary check with no acreage/percentage math. `inside_rural_
        study_area` is set per-county by the caller based on a direct
        comprehensive-plan review — see scan_orchestrator.py for the
        per-county sourcing.

    Fixed 2026-07-06 (late-late session):
    - Option 3 now credits interstate frontage toward the 75% test.
      The statute's (c)1.c reads "A combination of an interstate highway
      AND a parcel or parcels that are within an urban service district"
      -- so the interstate segment counts alongside the qualifying FLUM
      neighbors, not just as a boolean gate. Uses interstate_frontage_pct
      passed in from roads_client.measure_interstate_frontage_meters, and
      caps the combined total at 100 (a parcel with both high FLUM and
      high interstate frontage shouldn't overflow).
    - Option 4 now uses a real usb_perimeter_pct instead of the boolean
      adjacent_to_usb. The statute's (c)2 second clause reads "the parcel
      or parcels are surrounded on at least 50 percent of their perimeter
      by a parcel or parcels within an urban service district" -- a real
      >=50% test, not just adjacency. usb_perimeter_pct comes from
      roads_client.measure_usb_perimeter_meters.
    """
    pathways: list[int] = []

    if encirclement.pct_qualifying >= 75:
        pathways.append(1)

    if (
        designated_pct_existing_development is not None
        and encirclement.pct_qualifying >= 75
        and designated_pct_existing_development >= 75
    ):
        pathways.append(2)

    # Option 3 (c)1.c: 75% = interstate + qualifying-FLUM combined. Still
    # gate on adjacent_to_usb since the statute specifies the FLUM portion
    # must be USB-designated -- our current pct_qualifying doesn't
    # distinguish USB-designated FLUM from other qualifying FLUM, so this
    # gate is a defense-in-depth check; the "combined 75%" is what
    # actually determines this pathway.
    option3_combined_pct = min(100.0, encirclement.pct_qualifying + interstate_frontage_pct)
    if adjacent_to_interstate and adjacent_to_usb and option3_combined_pct >= 75:
        pathways.append(3)

    # Option 4 (c)2: two separate >=50% tests -- >=50% designated-for-dev
    # perimeter AND >=50% USB perimeter. pct_qualifying is the first
    # (residential/commercial/industrial FLUM proxy for designated-dev);
    # usb_perimeter_pct is the second, replacing the pre-2026-07-06
    # boolean adjacent_to_usb check that was over-inclusive.
    if acreage <= 700 and encirclement.pct_qualifying >= 50 and usb_perimeter_pct >= 50:
        pathways.append(4)

    if inside_rural_study_area:
        pathways.append(5)

    return pathways
