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

# Right-of-way / canal handling: per s. 163.3164(4), F.S., if a road,
# right-of-way, body of water, or canal runs along the perimeter, the
# calculation should be based on the parcel ACROSS that feature, not
# the right-of-way itself. This module's MVP version does not yet
# implement that substitution — see the TODO in classify_perimeter().


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


def esri_json_to_shapely(esri_geom: dict):
    """
    Convert an ArcGIS REST 'rings' polygon geometry to a Shapely Polygon.
    ArcGIS returns rings as lists of [x, y] pairs; the first ring is the
    exterior, subsequent rings are holes. This does not yet handle
    multipart polygons with multiple exterior rings (true MultiPolygon
    parcels) — common enough for parcels split by a road that this
    should be added before relying on this for a full county run.
    """
    _require_shapely()
    rings = esri_geom.get("rings", [])
    if not rings:
        raise ValueError("Geometry has no rings")
    exterior = rings[0]
    holes = rings[1:] if len(rings) > 1 else []
    return Polygon(exterior, holes)


def get_centroid_lat_lon(esri_geom: dict, source_wkid: int = 3086) -> Optional[tuple[float, float]]:
    """
    Extract a parcel's centroid as (lat, lon) in WGS84, for map display
    in the frontend. The statewide cadastral layer's native spatial
    reference is WKID 3086 (Florida Albers, in meters) — this requires
    a real coordinate transformation to WGS84 (EPSG:4326), not a
    hand-rolled approximation. Uses pyproj, which wraps the same PROJ
    library GIS software relies on for this exact operation.

    Requires: pyproj (add to requirements.txt: pyproj>=3.6)
    """
    _require_shapely()
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise ImportError(
            "pyproj is required for coordinate transformation. "
            "Install with: pip install pyproj"
        ) from exc

    try:
        poly = esri_json_to_shapely(esri_geom)
        centroid = poly.centroid
        x, y = centroid.x, centroid.y
    except (ValueError, TypeError, AttributeError):
        return None

    # Transformer.from_crs handles the real projection math via PROJ;
    # always_xy=True keeps input/output in (x,y) / (lon,lat) order
    # rather than PROJ's sometimes-confusing default axis ordering.
    transformer = Transformer.from_crs(
        f"EPSG:{source_wkid}", "EPSG:4326", always_xy=True
    )
    lon, lat = transformer.transform(x, y)
    return (lat, lon)


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

        shared = boundary.intersection(neighbor_poly.boundary)
        shared_length = shared.length
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
) -> list[int]:
    """
    Map an encirclement result plus a few other facts onto the five
    statutory pathways in s. 163.3164(4)(c), F.S. Pathway numbers match
    the order used elsewhere in this project (and in the bill itself):

      1. >=75% perimeter, existing industrial/commercial/residential
         development.
      2. >=75% perimeter, FLUM-designated development AND >=75% of
         those designated parcels already have existing development.
         NOTE: the 75% figure here was corrected from an earlier draft
         that used 50% — the enrolled bill text requires 75%, not 50%.
      3. Combination of an interstate highway and parcels within an
         urban service district/area/line designated for development.
      4. Parcel(s) <=700 acres, with the perimeter split between
         designated-development parcels (>=50%) and parcels within a
         USB (>=50%) — these can be the same or different segments.
      5. Located within an established rural study area in the local
         comprehensive plan — this requires a dataset this module does
         not yet fetch (no statewide or even consistently-named county
         layer was found for "rural study area" boundaries during
         research); always returns False here until that data source
         is identified per county.
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

    if adjacent_to_interstate and adjacent_to_usb and encirclement.pct_qualifying >= 75:
        pathways.append(3)

    if acreage <= 700 and encirclement.pct_qualifying >= 50 and adjacent_to_usb:
        pathways.append(4)

    return pathways
