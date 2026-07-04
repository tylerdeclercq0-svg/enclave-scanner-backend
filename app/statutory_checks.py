"""
Statutory-gap checks researched 2026-07-04/05 and wired in this pass:
  1. Unincorporated-status hard filter (s. 163.3164(4), F.S. applies to
     unincorporated agricultural enclaves only).
  2. Post-1/1/2025 single-owner ownership-change flag.

Per-county strategy for each is chosen in county_registry.py
(`unincorporated_check`, `sale_date_encoding`) based on a live research
pass -- see STATUS.md's "Statutory gaps" section for the full data-source
trail, including the Pasco inference that was live-query-DISPROVEN on
2026-07-05 (Port Richey City Hall intersects a real FLUM feature, so that
county's layer is not unincorporated-only after all).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from arcgis_client import query_layer
from county_registry import CountyEndpoint


CUTOFF_DATE = date(2025, 1, 1)

# Excel/OLE serial date epoch (day 0 = 1899-12-30, the traditional
# Lotus-1-2-3-compatible off-by-two-from-1900 convention).
_EXCEL_SERIAL_EPOCH = date(1899, 12, 30)
_EXCEL_SERIAL_CUTOFF = (CUTOFF_DATE - _EXCEL_SERIAL_EPOCH).days

_EPOCH_MILLIS_CUTOFF = (CUTOFF_DATE - date(1970, 1, 1)).days * 86_400_000


def sold_on_or_after_cutoff(county: CountyEndpoint, attrs: dict) -> Optional[bool]:
    """
    True/False if a post-1/1/2025 ownership change is determinable from
    this county's parcel-layer sale-date field(s) for this row. None if
    the county has no sale_date_encoding set, or the relevant field(s)
    are missing/unparseable on this particular row -- don't guess, leave
    it for manual review instead.
    """
    encoding = county.sale_date_encoding

    if encoding == "ymd_ints":
        year = attrs.get(county.sale_year_field)
        month = attrs.get(county.sale_month_field)
        day = attrs.get(county.sale_day_field)
        if year is None or month is None or day is None:
            return None
        try:
            return (int(year), int(month), int(day)) >= (2025, 1, 1)
        except (TypeError, ValueError):
            return None

    if encoding == "year_only":
        year = attrs.get(county.sale_year_field)
        if year is None:
            return None
        try:
            return int(year) >= 2025
        except (TypeError, ValueError):
            return None

    if encoding == "excel_serial":
        raw = attrs.get(county.sale_date_field)
        if raw is None:
            return None
        try:
            return int(raw) >= _EXCEL_SERIAL_CUTOFF
        except (TypeError, ValueError):
            return None

    if encoding == "epoch_millis":
        raw = attrs.get(county.sale_date_field)
        if raw is None:
            return None
        try:
            return int(raw) >= _EPOCH_MILLIS_CUTOFF
        except (TypeError, ValueError):
            return None

    return None


def check_unincorporated(
    county: CountyEndpoint,
    geometry: Optional[dict],
    area_sr: int,
) -> tuple[Optional[bool], str]:
    """
    Returns (is_unincorporated, detail) for the unincorporated-status hard
    filter. is_unincorporated is True/False if determinable, or None if
    this county has no automated check (manual_only) or the parcel has no
    geometry to spatially join. detail is always a human-readable string
    suitable for an exclusion_flags or needs_manual_review entry.
    """
    mode = county.unincorporated_check

    if mode == "already_filtered":
        return True, (
            "County's FLUM layer is pre-scoped to unincorporated land at "
            "the source -- no spatial join needed."
        )

    if mode == "manual_only":
        return None, (
            "Unincorporated-status check not automated for this county -- "
            "a live query disproved the assumption that this county's "
            "FLUM layer is unincorporated-only by construction (see "
            "STATUS.md); confirm manually."
        )

    if geometry is None:
        return None, (
            "No geometry available to spatially join against the FLUM "
            "layer for the unincorporated-status check -- verify manually."
        )

    # ArcGIS Server doesn't include spatialReference on a feature's
    # geometry in a /query response -- every candidate geometry passed in
    # here comes from parcel_fetcher, which always requests
    # outSR=AREA_SR, so that's the correct SR to assert, not a guess (same
    # fix as scan_orchestrator._buffer_esri_geometry).
    geometry_with_sr = dict(geometry)
    geometry_with_sr["spatialReference"] = {"wkid": area_sr}

    if mode == "flum_jurisdiction_join":
        features = list(query_layer(
            county.flum_service_url,
            geometry=geometry_with_sr,
            geometry_type="esriGeometryPolygon",
            spatial_rel="esriSpatialRelIntersects",
            out_fields=county.jurisdiction_field,
            out_sr=area_sr,
        ))
        if not features:
            return None, (
                "No FLUM polygon intersects this parcel's geometry -- "
                "unincorporated-status could not be determined; verify "
                "manually."
            )
        values = {f.get("attributes", {}).get(county.jurisdiction_field) for f in features}
        if values == {"Unincorporated"}:
            return True, "FLUM spatial join confirms Unincorporated jurisdiction."
        return False, (
            "FLUM spatial join found non-unincorporated jurisdiction "
            f"value(s) {sorted(v for v in values if v is not None)} -- "
            "this parcel likely falls within incorporated city limits."
        )

    if mode == "flum_incorporated_flu_exclude":
        features = list(query_layer(
            county.flum_service_url,
            geometry=geometry_with_sr,
            geometry_type="esriGeometryPolygon",
            spatial_rel="esriSpatialRelIntersects",
            out_fields=county.flu_field,
            out_sr=area_sr,
        ))
        if not features:
            return None, (
                "No FLUM polygon intersects this parcel's geometry -- "
                "unincorporated-status could not be determined; verify "
                "manually."
            )
        values = {f.get("attributes", {}).get(county.flu_field) for f in features}
        incorporated_hits = values & set(county.incorporated_flu_values)
        if incorporated_hits:
            return False, (
                "FLUM spatial join found incorporated-city FLU category "
                f"value(s) {sorted(incorporated_hits)} -- this parcel "
                "likely falls within incorporated city limits."
            )
        return True, "FLUM spatial join found no incorporated-city FLU category overlap."

    return None, f"Unknown unincorporated_check mode '{mode}' -- verify manually."
