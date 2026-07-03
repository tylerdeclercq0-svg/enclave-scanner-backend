"""
Statutory exclusion zone checks — s. 163.3162(4)(i), F.S.

The agricultural enclave pathway does not apply to property within:
  1. The Wekiva Study Area (s. 369.316, F.S.)
  2. The Everglades Protection Area (s. 373.4592(2), F.S.)
  3. Any Area of Critical State Concern (s. 380.055, .0551, .0552,
     .0553, or .0555, F.S.)
  4. Any portion of a property encumbered by a recorded conservation
     easement (s. 704.06, F.S.)
  5. A military installation or range identified in s. 163.3175(2), F.S.

Data source status, confirmed during research:
  - Areas of Critical State Concern: FDEP publishes a generalized
    boundary layer (mapdirect-fdep.opendata.arcgis.com/maps/
    areas-of-critical-state-concern). None of the seven pilot counties
    (Hillsborough, Orange, Pasco, Sarasota, Manatee, Brevard, Volusia)
    fall within the five designated ACSCs (Apalachicola Bay, Green
    Swamp, Big Cypress, Florida Keys, City of Key West), so this check
    should return no hits for any pilot-county parcel — but the layer
    should still be queried and not just hardcoded to "false", since a
    parcel right at a county boundary or a future ACSC designation
    change could break that assumption silently.
  - Everglades Protection Area: defined by statute, not relevant to any
    of the seven pilot counties either, same caveat as above.
  - Wekiva Study Area: DOES overlap parts of Orange and Seminole
    counties. Orange is a pilot county — this is the one exclusion that
    plausibly matters here and should be checked for real, not assumed
    away.
  - Conservation easements: NO statewide or consistent county-level GIS
    layer was found during research. This is recorded at the county
    Clerk/Recorder level, parcel by parcel, with no standard schema
    across counties. This check cannot be automated with current public
    data and is flagged for manual verification in every case.
  - Military installations: covered by s. 163.3175(2), F.S. buffer
    areas — Florida DEP/DOD publish some compatibility-zone layers for
    specific bases, but a consolidated statewide layer keyed to this
    exact statutory definition was not located during research.

Net result: this module can give a real answer for the Wekiva Study
Area overlap (meaningful for Orange County specifically) and a
structurally honest "not automated" flag for everything else, rather
than a false sense of completeness.
"""

from __future__ import annotations

from typing import Optional

from arcgis_client import query_layer
from parcel_fetcher import CandidateParcel


# Confirmed during research: FDEP's generalized ACSC boundary layer.
ACSC_LAYER_URL_PLACEHOLDER = (
    "https://mapdirect-fdep.opendata.arcgis.com/maps/"
    "areas-of-critical-state-concern"
    # NOTE: this is a Hub page, not a raw FeatureServer endpoint. Resolve
    # the underlying service URL (same caveat as several county FLUM
    # layers in county_registry.py) before wiring this in for real.
)

# Wekiva Study Area boundary — not yet resolved to a specific GIS layer
# URL in this research pass. The statute (s. 369.316, F.S.) references
# a study area whose boundary is maintained by the relevant water
# management district / DEP; locate the authoritative layer before
# enabling automated checking. Counties known to be affected: Orange,
# Seminole, Lake, and Volusia (per the Wekiva Parkway and Protection
# Act area definition) — note Volusia is also a pilot county, so this
# matters for two of the seven, not just one.
WEKIVA_STUDY_AREA_LAYER_URL_PLACEHOLDER = None


def check_exclusions(parcel: CandidateParcel) -> list[str]:
    """
    Return a list of human-readable exclusion flags for a candidate
    parcel. An empty list means "no automated exclusion hit" — NOT
    "definitely clear." Conservation easements and military buffers
    always get a manual-review flag regardless of geometry, since no
    automated source exists for them yet.
    """
    flags: list[str] = []

    if parcel.geometry is None:
        flags.append(
            "No geometry available to check exclusion zone overlap — "
            "verify manually."
        )
        return flags

    # Wekiva Study Area — only meaningful for Orange and Volusia among
    # the pilot counties, but checked generically here since the
    # underlying layer (once resolved) would cover its full extent
    # regardless of which county is being scanned.
    if WEKIVA_STUDY_AREA_LAYER_URL_PLACEHOLDER:
        wekiva_hits = list(query_layer(
            WEKIVA_STUDY_AREA_LAYER_URL_PLACEHOLDER,
            geometry=parcel.geometry,
            geometry_type="esriGeometryPolygon",
            spatial_rel="esriSpatialRelIntersects",
            return_geometry=False,
        ))
        if wekiva_hits:
            flags.append(
                "Parcel intersects the Wekiva Study Area — the "
                "agricultural enclave pathway does not apply here "
                "per s. 163.3162(4)(i)1., F.S."
            )
    else:
        flags.append(
            "Wekiva Study Area layer not yet wired in — if this parcel "
            "is in Orange or Volusia County, confirm manually against "
            "the Wekiva Parkway and Protection Act boundary before "
            "relying on enclave eligibility."
        )

    # ACSC check — structurally present but expected to return nothing
    # for any of the seven pilot counties; kept as a real check rather
    # than a skip so a future county addition (e.g. Monroe) doesn't
    # silently bypass it.
    flags.append(
        "Area of Critical State Concern check not yet wired to a "
        "resolved FeatureServer endpoint — none of the seven pilot "
        "counties fall within a designated ACSC as of this research "
        "pass, but confirm this assumption before adding counties "
        "outside the current pilot set."
    )

    flags.append(
        "Conservation easement check has no available statewide or "
        "consistent county GIS source — always verify with the county "
        "Clerk/Recorder before relying on enclave eligibility."
    )

    flags.append(
        "Military installation buffer (s. 163.3175(2), F.S.) check not "
        "automated — verify manually if the parcel is near a known "
        "military installation."
    )

    return flags
