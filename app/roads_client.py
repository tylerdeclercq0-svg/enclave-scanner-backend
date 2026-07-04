"""
FDOT interstate-adjacency check + a Pasco-only USB approximation — real
data behind encirclement Options C/D.

Options 3 (interstate + USB combination) and 4 (<=700 ac, planned-
development + USB) both need to know whether a candidate parcel is
adjacent to an interstate highway and to an Urban Service Area (USB).

Interstate adjacency (check_adjacent_to_interstate): a real, live,
statewide signal as of 2026-07-06 — FDOT's own hosted ArcGIS Server,
`RCI_Layers/FeatureServer/7` ("Interstates"), a real statewide polyline
layer whose `COUNTY` field (plain English county name, e.g. "Pasco",
"St. Johns") matches this project's own county_registry.py `name` field
for all four pilot counties (Pasco: I-75/I-275, Nassau: I-10, St. Johns:
I-95, Osceola: I-4).

USB adjacency (check_adjacent_to_usb): still only real for Pasco, and
even there it's an approximation, not a from-the-source USB layer — see
county_registry.py's `rural_area_layer_url` docstring. Searched live for
a dedicated USB or comparable layer for Nassau, St. Johns, and Osceola;
found nothing (only Hillsborough is confirmed, per county_registry.py's
pre-existing note, to have a real one). This means Options C/D remain
unreachable for Nassau/St. Johns/Osceola, and reachable-in-principle
(via an approximation) only for Pasco.
"""

from __future__ import annotations

from typing import Optional

from arcgis_client import query_layer
from parcel_fetcher import AREA_SR

try:
    from shapely.geometry import MultiPolygon
except ImportError:  # pragma: no cover
    MultiPolygon = None  # type: ignore


INTERSTATES_LAYER_URL = "https://gis.fdot.gov/arcgis/rest/services/RCI_Layers/FeatureServer/7"

# How far past the candidate parcel's boundary to look for an interstate
# centerline — same order of magnitude as the FLUM-neighbor buffer used
# elsewhere in this project (scan_orchestrator.py's fetch_neighbor_buffer_feet
# default), since "adjacent to an interstate" is the same kind of
# real-world-adjacency-despite-digitization-gap problem.
INTERSTATE_ADJACENCY_BUFFER_FEET = 50.0


def _buffer_geometry_feet(geometry: dict, distance_feet: float) -> dict:
    """
    Buffer an ArcGIS-format polygon geometry (already in AREA_SR, meters)
    outward by distance_feet, returning a new ArcGIS-format geometry.
    Duplicated in miniature from scan_orchestrator._buffer_esri_geometry
    rather than imported from it, to avoid a circular import
    (scan_orchestrator will call into this module).
    """
    from encirclement import esri_json_to_shapely

    shapely_geom = esri_json_to_shapely(geometry)
    distance_meters = distance_feet * 0.3048
    buffered = shapely_geom.buffer(distance_meters)

    if MultiPolygon is not None and isinstance(buffered, MultiPolygon):
        rings = [list(part.exterior.coords) for part in buffered.geoms]
    else:
        rings = [list(buffered.exterior.coords)]

    return {
        "rings": rings,
        "spatialReference": {"wkid": AREA_SR},
    }


def check_adjacent_to_usb(geometry: dict, rural_area_layer_url: Optional[str]) -> bool:
    """
    Approximate "adjacent to the Urban Service Area" using a county's
    Rural Area layer as the complement, where available (currently only
    Pasco — see county_registry.py's rural_area_layer_url docstring for
    why this is an approximation, not a from-the-source USB layer).
    Returns False outright if no rural_area_layer_url is configured for
    this county (the honest "no data" case, matching every other
    county), rather than guessing.

    Buffers the candidate parcel slightly, then checks whether that
    buffered shape is ENTIRELY within a single Rural Area polygon
    (esriSpatialRelWithin). If so, the parcel is deep in rural land, not
    touching the Urban Service Area — False. If the buffered shape spans
    outside every Rural Area polygon, it's treated as touching the Urban
    Service Area — True. Confirmed live: a point built inside a real
    Rural Area polygon correctly returns a `within` hit; a point in
    downtown New Port Richey (clearly urban) correctly returns none.
    """
    if rural_area_layer_url is None:
        return False

    buffered = _buffer_geometry_feet(geometry, INTERSTATE_ADJACENCY_BUFFER_FEET)
    hits = list(query_layer(
        rural_area_layer_url,
        geometry=buffered,
        geometry_type="esriGeometryPolygon",
        spatial_rel="esriSpatialRelWithin",
        out_fields="OBJECTID",
        return_geometry=False,
    ))
    return not bool(hits)


def check_adjacent_to_interstate(geometry: dict, county_name: str) -> bool:
    """
    True if the candidate parcel's (buffered) boundary intersects an FDOT
    interstate centerline within the given county. `county_name` must
    match FDOT's own `COUNTY` field spelling (this project's
    CountyEndpoint.name values already match, per this module's
    docstring) — an unrecognized county name will simply find zero
    interstates in that county and correctly return False, not error.
    """
    buffered = _buffer_geometry_feet(geometry, INTERSTATE_ADJACENCY_BUFFER_FEET)
    where = f"COUNTY='{county_name}'"
    hits = list(query_layer(
        INTERSTATES_LAYER_URL,
        where=where,
        geometry=buffered,
        geometry_type="esriGeometryPolygon",
        spatial_rel="esriSpatialRelIntersects",
        out_fields="ROUTE",
        return_geometry=False,
    ))
    return bool(hits)
