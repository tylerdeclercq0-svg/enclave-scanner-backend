"""
Water/wastewater service estimate — Florida Water Management Inventory
(FLWMI), Florida Department of Health.

Real, live, statewide parcel-level layer confirmed 2026-07-06:
    https://gis.floridahealth.gov/server/rest/services/FLWMI/FLWMI_Wastewater/MapServer/0

Confirmed live via describe_layer() + real sample queries:
  - `CO_NO` is a 2-character ZERO-PADDED STRING DOR county code (its own
    coded-value domain lists e.g. Pasco='61', Osceola='59', Nassau='55',
    St. Johns='65') -- NOT an int. These are the same DOR county numbers
    already stored as CountyEndpoint.fips in county_registry.py, so
    `f"{county.fips:02d}"` is the correct value, not a new lookup.
  - `WW` (wastewater) and `DW` (drinking water) are coded-value strings
    with a confidence qualifier baked into the code itself: Known / Likely
    / "SWL" (Somewhat Likely), e.g. `KnownSewer`, `LikelyWell`,
    `SWLPublic`, plus `UNDT` (undetermined/conflicting), `UNK` (no data),
    `NA` (not built). Decoded to human labels + a separate confidence
    tier below, matching the FLWMI's own domain descriptions exactly (do
    not rephrase these -- they're the source's own defined meanings).
  - `PARCELNO` is the join key. Confirmed live, real samples:
      Pasco:   "01-24-16-0000-00100-0000" -- identical format to Pasco's
               own ParcelID field, no transform needed.
      Osceola: "012527000000400000" (no dashes) -- identical to
               Osceola's own PARCELNO field, no transform needed.
      Nassau:  "00-00-30-0020-0001-0000" -- same dash pattern as its own
               PARCELID; assumed direct match (not yet cross-checked
               against one specific real Nassau PARCELID sample).
      St. Johns: bare 10-digit string ("0000200010"), but this county's
               own PIN field is space-separated ("010832 0010") -- see
               `CountyEndpoint.flwmi_parcel_id_transform`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

from county_registry import CountyEndpoint

FLWMI_WASTEWATER_LAYER_URL = (
    "https://gis.floridahealth.gov/server/rest/services/"
    "FLWMI/FLWMI_Wastewater/MapServer/0"
)

# Decoded straight from the layer's own coded-value domains (confirmed
# live via describe_layer()) -- not paraphrased.
_WW_LABELS = {
    "KnownSeptic": "Known Onsite Septic",
    "KnownSewer": "Known Central Sewer",
    "LikelySeptic": "Likely Onsite Septic",
    "LikelySewer": "Likely Central Sewer",
    "SWLSeptic": "Somewhat Likely Onsite Septic",
    "SWLSewer": "Somewhat Likely Central Sewer",
    "UNDT": "Undetermined, conflicting data",
    "UNK": "Unknown, no data",
    "NA": "N/A, not built",
}
_DW_LABELS = {
    "KnownWell": "Known Private Well",
    "KnownPublic": "Known Public Water",
    "LikelyWell": "Likely Private Well",
    "LikelyPublic": "Likely Public Water",
    "SWLWell": "Somewhat Likely Private Well",
    "SWLPublic": "Somewhat Likely Public Water",
    "UNDT": "Undetermined, conflicting data",
    "UNK": "Unknown, no data",
    "NA": "N/A, not built",
}

# Confidence tier derived from the code's own prefix -- "Known" > "Likely"
# > "Somewhat Likely" > everything else (UNDT/UNK/NA all mean "no usable
# signal", tiered together as "Unknown").
_CONFIDENCE_BY_PREFIX = [
    ("Known", "Known"),
    ("Likely", "Likely"),
    ("SWL", "Somewhat Likely"),
]


def _confidence_for_code(code: Optional[str]) -> str:
    if not code:
        return "Unknown"
    for prefix, tier in _CONFIDENCE_BY_PREFIX:
        if code.startswith(prefix):
            return tier
    return "Unknown"  # UNDT, UNK, NA


def _normalize_parcel_id(county: CountyEndpoint, parcel_id: str) -> str:
    if county.flwmi_parcel_id_transform == "strip_spaces":
        return parcel_id.replace(" ", "")
    return parcel_id


def _query_flwmi(where: str, out_fields: str) -> list[dict[str, Any]]:
    """
    Direct GET against the FLWMI layer, deliberately NOT using
    arcgis_client.query_layer's POST-based request. Confirmed live
    2026-07-06: this specific FDOH-hosted ArcGIS Server only honors
    `f=json` as a URL query parameter -- POSTing it in the request body
    (which every other ArcGIS host in this project accepts fine) makes
    this host silently return the HTML "ArcGIS REST Services Directory"
    page instead of JSON, with a 200 status (no exception raised by
    raise_for_status(), so this failure mode is silent unless you
    inspect the body). GET works cleanly here since this lookup never
    sends a geometry filter, so query_layer's POST-for-8KB-URL-limit
    rationale doesn't apply to this use case.
    """
    resp = requests.get(
        FLWMI_WASTEWATER_LAYER_URL + "/query",
        params={
            "where": where,
            "outFields": out_fields,
            "f": "json",
            "returnGeometry": "false",
            "resultRecordCount": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"FLWMI query error: {payload['error']}")
    return payload.get("features", [])


@dataclass
class WaterSewerResult:
    water_source: Optional[str]  # human label, e.g. "Likely Public Water"
    wastewater_method: Optional[str]  # human label, e.g. "Known Central Sewer"
    # Overall confidence tier: the WORSE (lower) of the water and
    # wastewater confidence tiers, since a caller relying on this
    # estimate needs the weaker of the two signals, not the stronger.
    confidence: str  # "Known" | "Likely" | "Somewhat Likely" | "Unknown"
    found: bool  # False if no FLWMI record joined for this parcel at all


_CONFIDENCE_RANK = {"Known": 3, "Likely": 2, "Somewhat Likely": 1, "Unknown": 0}


def lookup_water_sewer(county: CountyEndpoint, parcel_id: str) -> WaterSewerResult:
    """
    Look up this parcel's real, FDOH-sourced water source and wastewater
    disposal method estimate. Returns found=False (not an exception) if
    no FLWMI record joins to this parcel_id -- that's a real, expected
    outcome for parcels FLWMI hasn't inventoried, not a query failure.
    """
    if not parcel_id:
        return WaterSewerResult(water_source=None, wastewater_method=None, confidence="Unknown", found=False)

    co_no = f"{county.fips:02d}"
    normalized_id = _normalize_parcel_id(county, parcel_id)
    # Escape single quotes defensively -- parcel IDs are alphanumeric/
    # dash/space in every county sampled so far, but this is a WHERE
    # clause built from external data.
    escaped_id = normalized_id.replace("'", "''")

    features = _query_flwmi(
        where=f"CO_NO='{co_no}' AND PARCELNO='{escaped_id}'",
        out_fields="WW,DW",
    )
    if not features:
        return WaterSewerResult(water_source=None, wastewater_method=None, confidence="Unknown", found=False)

    attrs = features[0].get("attributes", {})
    ww_code = attrs.get("WW")
    dw_code = attrs.get("DW")

    ww_label = _WW_LABELS.get(ww_code)
    dw_label = _DW_LABELS.get(dw_code)

    ww_confidence = _confidence_for_code(ww_code)
    dw_confidence = _confidence_for_code(dw_code)
    overall_confidence = min(
        (ww_confidence, dw_confidence),
        key=lambda tier: _CONFIDENCE_RANK[tier],
    )

    return WaterSewerResult(
        water_source=dw_label,
        wastewater_method=ww_label,
        confidence=overall_confidence,
        found=True,
    )
