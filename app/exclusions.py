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
  - Areas of Critical State Concern: RESOLVED 2026-07-06 — the Hub page
    (mapdirect-fdep.opendata.arcgis.com/maps/areas-of-critical-state-concern)
    is not itself a queryable endpoint, but its underlying FeatureServer is:
    ca.dep.state.fl.us/arcgis/rest/services/Map_Direct/Program_Support/
    MapServer/5, found via ArcGIS Online's item search API (owner
    "FDEPMapDirect"). Confirmed live: 5 real features (Apalachicola,
    Green Swamp, Florida Keys, Key West, Big Cypress — matching this
    module's own long-standing citation), none listing any of the seven
    pilot counties (Hillsborough, Orange, Pasco, Sarasota, Manatee,
    Brevard, Volusia) in their `CNTYS` field. Now wired in as a real
    automated check below, not a permanent manual-review note.
  - Everglades Protection Area: confirmed live 2026-07-04 — a real
    SFWMD-hosted FeatureServer layer (6 features), matching the cited
    statute (s. 373.4592(2), F.S. / Ch. 40E-63, F.A.C.). Not
    geographically relevant to any of the four current pilot counties,
    but wired in as a real layer, not a placeholder, per the same
    caveat as ACSC above.
  - Wekiva Study Area: confirmed live 2026-07-04 — s. 369.316, F.S.
    specifically cites the Wekiva **Study** Area, legally DISTINCT from
    the more commonly-indexed "Wekiva River Protection Area" (WRPA, s.
    369.303/369.301(9)). Seminole County's own layer has explicit
    separate WSA/WRPA yes/no fields on the same features — filtering
    WSA='yes' gets the actual statutory boundary. Caveat: this layer's
    extent looks like it covers only the Orange/Seminole border area
    and may not capture the statute's Lake County portion — moot for
    the four current pilot counties (none are in Lake/Orange/Seminole).
  - Conservation easements: NO statewide or consistent county-level GIS
    layer was found during research. This is recorded at the county
    Clerk/Recorder level, parcel by parcel, with no standard schema
    across counties. This check cannot be automated with current public
    data and is flagged for manual verification in every case.
  - Military installations: covered by s. 163.3175(2), F.S. buffer
    areas — Florida DEP/DOD publish some compatibility-zone layers for
    specific bases, but a consolidated statewide layer keyed to this
    exact statutory definition was not located during research.
"""

from __future__ import annotations

import threading
from typing import Optional

from arcgis_client import query_layer
from parcel_fetcher import CandidateParcel, AREA_SR


# ---------------------------------------------------------------------
# Fix A (2026-07-06 pass): cache the three statewide exclusion-layer
# geometries once per Python process, then do point-in-polygon locally.
# Rationale from the profiling data: pre-fix, each of Wekiva/Everglades/
# ACSC was ~800 ms per parcel of remote-query latency, contributing 60 s
# per 25-parcel batch (~30% of total wall clock) for what are three
# fixed, statute-defined boundary sets (Wekiva 5, Everglades 6, ACSC 5
# polygons total, statewide, stable over the life of the statute).
# Post-fix, we hit each remote once per process, then check every parcel
# with local shapely `.intersects()` for microseconds. The FDEP ACSC
# server -- the flakiest of the three, source of transient ReadTimeouts
# elsewhere in this project -- gets hit exactly once instead of once
# per parcel.
#
# Cache scope: module-level, protected by a lock so concurrent workers
# don't race the warm-up. Persistent for the lifetime of the process;
# if the underlying statute-defined boundaries ever change, a redeploy
# refreshes the cache. No TTL (data isn't time-sensitive at that scale).
# ---------------------------------------------------------------------
_CACHE_LOCK = threading.Lock()
_wekiva_polys_cache: Optional[list] = None
_everglades_polys_cache: Optional[list] = None
_acsc_polys_cache: Optional[list] = None  # each entry: (shapely_poly, name)


# Confirmed live 2026-07-04: SFWMD-hosted, 6 real features, matches
# s. 373.4592(2), F.S. / Ch. 40E-63, F.A.C.
EVERGLADES_PROTECTION_AREA_LAYER_URL = (
    "https://services1.arcgis.com/sDAPyc2rGRn7vf9B/arcgis/rest/services/"
    "RULE40E_63_EVERGLADES_PROTECTION_AREA/FeatureServer/0"
)

# Wekiva STUDY Area (s. 369.316, F.S.) — confirmed live 2026-07-04 via
# Seminole County's own layer, which has explicit separate WSA/WRPA
# yes/no fields on the same 2 real features (e.g. one feature is
# WSA=yes, WRPA=no, ~19,739 acres). Filtering WSA='yes' avoids the real
# conflation trap of wiring in a WRPA-only layer instead (the first
# search results found — an Orange County layer, an SJRWMD layer — were
# both WRPA, a legally distinct area under a different part of the same
# statute chapter).
WEKIVA_STUDY_AREA_LAYER_URL = (
    "https://services3.arcgis.com/n4VF6lyYfB5kizho/arcgis/rest/services/"
    "WekivaProtectionAreas/FeatureServer/0"
)
WEKIVA_STUDY_AREA_FIELD = "WSA"
WEKIVA_STUDY_AREA_VALUE = "yes"

# Areas of Critical State Concern — confirmed live 2026-07-06, real FDEP
# FeatureServer (5 real features: Apalachicola, Green Swamp, Florida Keys,
# Key West, Big Cypress), resolved from the Hub page via ArcGIS Online's
# item search API.
ACSC_LAYER_URL = (
    "https://ca.dep.state.fl.us/arcgis/rest/services/Map_Direct/"
    "Program_Support/MapServer/5"
)


def _with_area_sr(geometry: dict) -> dict:
    """
    ArcGIS Server doesn't include spatialReference on a feature's
    geometry in a /query response (only once, at the FeatureSet root,
    which arcgis_client.query_layer discards) — every candidate geometry
    passed into check_exclusions comes from parcel_fetcher, which always
    requests outSR=AREA_SR, so that's the correct SR to assert here, not
    a guess. Without this, query_layer's inSR silently falls back to
    4326 and misinterprets these projected-meter coordinates as
    lat/long — same bug class already fixed elsewhere in this project
    (scan_orchestrator._buffer_esri_geometry).
    """
    geometry_with_sr = dict(geometry)
    geometry_with_sr["spatialReference"] = {"wkid": AREA_SR}
    return geometry_with_sr


def standing_manual_notes() -> list[str]:
    """
    Permanent "not automated, always verify manually" reminders that
    apply to every parcel regardless of geometry or query results —
    conservation easements and military buffers (ACSC is now a real
    automated check below, as of 2026-07-06). These are NOT exclusion
    hits (they don't mean the parcel fails anything), so they belong in
    needs_manual_review, not exclusion_flags.

    FIXED 2026-07-06: these lines used to be appended directly inside
    check_exclusions()'s returned list, which meant exclusion_flags was
    NEVER actually empty for any real parcel — even when Wekiva/
    Everglades genuinely didn't hit. That silently broke the dashboard's
    "clear" vs "N EXCLUDED" distinction (every parcel showed as excluded)
    and made a "no manual review needed" confidence tier impossible to
    reach. Split out here so exclusion_flags means what it claims: a
    real, automated hard-exclusion hit, nothing else.
    """
    return [
        "Conservation easement check has no available statewide or "
        "consistent county GIS source — always verify with the county "
        "Clerk/Recorder before relying on enclave eligibility.",
        "Military installation buffer (s. 163.3175(2), F.S.) check not "
        "automated — verify manually if the parcel is near a known "
        "military installation.",
    ]


def _fetch_polygons_from_layer(
    layer_url: str,
    where: str = "1=1",
    out_fields: str = "OBJECTID",
) -> list[dict]:
    """
    Fetch every polygon feature from a layer as a list of dicts with
    geometry (in AREA_SR / WKID 3086) + attributes. Used by the three
    warm-up helpers below. Kept as a plain list of Esri-shape dicts so
    the shapely conversion happens once (in the cache-populate step),
    not per-parcel.
    """
    return list(query_layer(
        layer_url, where=where, out_fields=out_fields,
        return_geometry=True, out_sr=AREA_SR,
    ))


def _to_shapely(feature: dict):
    from encirclement import esri_json_to_shapely
    return esri_json_to_shapely(feature["geometry"])


def _warm_wekiva_cache() -> list:
    """Load + parse WSA='yes' polygons once; return the cached list."""
    global _wekiva_polys_cache
    with _CACHE_LOCK:
        if _wekiva_polys_cache is None:
            feats = _fetch_polygons_from_layer(
                WEKIVA_STUDY_AREA_LAYER_URL,
                where=f"{WEKIVA_STUDY_AREA_FIELD}='{WEKIVA_STUDY_AREA_VALUE}'",
                out_fields=WEKIVA_STUDY_AREA_FIELD,
            )
            polys = []
            for f in feats:
                try:
                    polys.append(_to_shapely(f))
                except (ValueError, TypeError):
                    continue
            _wekiva_polys_cache = polys
        return _wekiva_polys_cache


def _warm_everglades_cache() -> list:
    global _everglades_polys_cache
    with _CACHE_LOCK:
        if _everglades_polys_cache is None:
            feats = _fetch_polygons_from_layer(EVERGLADES_PROTECTION_AREA_LAYER_URL)
            polys = []
            for f in feats:
                try:
                    polys.append(_to_shapely(f))
                except (ValueError, TypeError):
                    continue
            _everglades_polys_cache = polys
        return _everglades_polys_cache


def _warm_acsc_cache() -> list:
    global _acsc_polys_cache
    with _CACHE_LOCK:
        if _acsc_polys_cache is None:
            feats = _fetch_polygons_from_layer(ACSC_LAYER_URL, out_fields="NAME")
            polys = []
            for f in feats:
                try:
                    name = str(f.get("attributes", {}).get("NAME") or "unknown")
                    polys.append((_to_shapely(f), name))
                except (ValueError, TypeError):
                    continue
            _acsc_polys_cache = polys
        return _acsc_polys_cache


def _reset_exclusion_caches() -> None:
    """Test helper: reset the three module-level caches to None."""
    global _wekiva_polys_cache, _everglades_polys_cache, _acsc_polys_cache
    with _CACHE_LOCK:
        _wekiva_polys_cache = None
        _everglades_polys_cache = None
        _acsc_polys_cache = None


def check_exclusions(parcel: CandidateParcel) -> list[str]:
    """
    Return a list of human-readable HARD exclusion flags for a candidate
    parcel — real, automated hits only (Wekiva/Everglades/ACSC
    intersection). An empty list means "no automated exclusion hit" —
    NOT "definitely clear." See standing_manual_notes() for the separate,
    always-present manual-verification reminders (easements/military)
    that used to be merged into this list.

    Per Fix A (2026-07-06): the three statewide layers get fetched once
    per process, then every parcel is checked locally with shapely
    intersects(). See the cache-warming helpers above for the rationale.
    """
    flags: list[str] = []

    if parcel.geometry is None:
        return flags

    # Sub-instrumentation (2026-07-06): so profiling can see the cached
    # exclusions check's real cost (should be effectively zero-remote
    # after the first parcel warms the caches).
    from scan_orchestrator import _time_block
    from encirclement import esri_json_to_shapely

    parcel_shape = esri_json_to_shapely(parcel.geometry)

    with _time_block("exclusions_wekiva"):
        wekiva_polys = _warm_wekiva_cache()
        wekiva_hit = any(parcel_shape.intersects(p) for p in wekiva_polys)
    if wekiva_hit:
        flags.append(
            "Parcel intersects the Wekiva Study Area (WSA='yes') — the "
            "agricultural enclave pathway does not apply here per "
            "s. 163.3162(4)(i)1., F.S."
        )

    with _time_block("exclusions_everglades"):
        everglades_polys = _warm_everglades_cache()
        everglades_hit = any(parcel_shape.intersects(p) for p in everglades_polys)
    if everglades_hit:
        flags.append(
            "Parcel intersects the Everglades Protection Area — the "
            "agricultural enclave pathway does not apply here per "
            "s. 373.4592(2), F.S."
        )

    with _time_block("exclusions_acsc"):
        acsc_polys = _warm_acsc_cache()
        acsc_hits = [name for poly, name in acsc_polys if parcel_shape.intersects(poly)]
    if acsc_hits:
        flags.append(
            f"Parcel intersects an Area of Critical State Concern "
            f"({', '.join(acsc_hits)}) — the agricultural enclave pathway "
            "does not apply here per s. 380.055 (and related sections), F.S."
        )

    return flags
