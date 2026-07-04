"""
Enclave Scanner API — FastAPI application.

This is the real HTTP layer the frontend calls instead of generating
mock data. It wraps the existing modules (county_registry, scan_orchestrator,
ring_demographics, etc.) that were built and unit-tested against mocked
ArcGIS/Census responses during earlier research passes.

STATUS: This has not been run against live data in this sandbox — there
is no outbound network access here. Deploy this to Render/Railway (or
any host with real internet access) and test against one resolved
county (start with Hillsborough or Brevard, the most fully-confirmed
endpoints) before trusting results for the others.

Run locally for testing:
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000

Endpoints:
    GET  /api/counties                    - list all counties + their live/pending status
    GET  /api/counties/{county_id}/scan    - run a scan (real ArcGIS calls)
    GET  /api/parcels/{parcel_id}/demographics - on-demand Census ring pull
    GET  /health                          - liveness check for the host platform
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))

from county_registry import COUNTIES, POPULATION_CAP  # noqa: E402
import scan_orchestrator  # noqa: E402
import ring_demographics  # noqa: E402
import diligence_tracker  # noqa: E402
import coverage_ledger  # noqa: E402
import zcta_client  # noqa: E402
from fastapi.responses import Response  # noqa: E402


app = FastAPI(
    title="Enclave Scanner API",
    description="Backend for Falcone Group's Florida agricultural enclave screening tool (SB 686 / Ch. 2026-34)",
    version="0.1.0",
)

# CORS: allow the Netlify-hosted frontend to call this API from a browser.
# Restrict allow_origins to your actual Netlify domain before going live —
# "*" is fine for local testing only, never for production.
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class CountyInfo(BaseModel):
    id: str
    name: str
    fips: int
    flum_service_url: str
    flu_field: str
    live: bool
    population: int
    notes: str


_CODE_VERSION_CACHE: Optional[str] = None


def _resolve_code_version() -> str:
    """
    Return the short git commit hash for the deployed build, so /health
    reflects the ACTUAL deployed commit instead of a hardcoded string that
    goes stale on every deploy. Render's build environment sets
    RENDER_GIT_COMMIT; local dev falls back to reading .git/HEAD.
    Cached at import time so a subsequent `.git` removal doesn't break it.
    """
    global _CODE_VERSION_CACHE
    if _CODE_VERSION_CACHE is not None:
        return _CODE_VERSION_CACHE
    version = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT")
    if version:
        _CODE_VERSION_CACHE = version[:7]
        return _CODE_VERSION_CACHE
    try:
        git_head_path = os.path.join(os.path.dirname(__file__), "..", ".git", "HEAD")
        with open(git_head_path) as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            ref_path = os.path.join(os.path.dirname(__file__), "..", ".git", head[5:])
            with open(ref_path) as f:
                _CODE_VERSION_CACHE = f.read().strip()[:7]
        else:
            _CODE_VERSION_CACHE = head[:7]
    except (OSError, IOError):
        _CODE_VERSION_CACHE = "unknown"
    return _CODE_VERSION_CACHE


@app.get("/health")
def health():
    """Liveness check — Render/Railway ping this to confirm the service is up."""
    return {"status": "ok", "code_version": _resolve_code_version()}


@app.get("/api/counties", response_model=list[CountyInfo])
def list_counties():
    """
    Return all counties in the registry with their resolution status.

    'live' reflects `CountyEndpoint.confirmed_live` — an explicit flag set
    per county based on whether its FLUM/parcel field names have actually
    been verified via a live describe_layer() call (see each county's own
    `notes`), not a heuristic. Orange, Sarasota, and Manatee are reachable
    endpoints but have unconfirmed field names, so they come back with
    live=False ("coming soon" in the UI) until a ground-truth pass
    confirms them, same standard as every other county here.
    """
    result = []
    for county_id, county in COUNTIES.items():
        result.append(CountyInfo(
            id=county.id,
            name=county.name,
            fips=county.fips,
            flum_service_url=county.flum_service_url,
            flu_field=county.flu_field,
            live=county.confirmed_live,
            population=county.population,
            notes=county.notes,
        ))
    return result


@app.get("/api/counties/{county_id}/scan")
def scan_county(
    county_id: str,
    min_acreage: float = Query(20.0, ge=0),
    max_acreage: float = Query(4480.0, gt=0, description="Statutory ceiling: 1,280 acres general cap, or 4,480 acres under the dense-urban exception in s. 163.3164(4)(e), F.S. Default 4,480 to include exception-eligible parcels; they're surfaced with a specific manual-review note explaining the buildout-density condition."),
    max_candidates: int = Query(25, ge=1, le=200, description="Caps how many parcels get the full (slower) encirclement check. Start small (10-25) for testing."),
    require_single_owner: bool = Query(False, description="Drop candidates with a recorded co-owner (owner_name_2 populated). Parcels where this county has no co-owner field at all are NOT dropped — unknowable, not assumed single-owner."),
    min_encirclement_pct: Optional[float] = Query(None, ge=0, le=100, description="Drop candidates below this qualifying-perimeter percentage."),
    flum_character: Optional[str] = Query(None, description="Exact-match filter on the candidate's own FLUM designation string."),
    surrounding_density: Optional[str] = Query(None, description="One of rural/suburban/urban/unknown — filters on the dominant neighboring FLU density bucket."),
):
    """
    Run a live scan against one county: pulls candidates from the
    statewide cadastral layer, runs the encirclement estimate against
    the county's FLUM layer, checks exclusions, and scores each result.

    This calls straight through to scan_orchestrator.run_county_scan(),
    which in turn makes real ArcGIS REST calls — expect this to take
    several seconds to tens of seconds depending on county size and
    how many candidate parcels survive the acreage filter. Consider
    adding a background job / polling pattern instead of a synchronous
    request if county scans start timing out on your hosting platform's
    request limit (Render's free tier caps around 30s, for example).
    """
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")

    # s. 163.3164(4)(f), F.S.: agricultural-enclave eligibility only
    # applies "within a county with a population of 1.75 million or
    # less." Every current registry entry is well under this, but check
    # explicitly rather than relying on that -- if the registry is ever
    # extended to a larger county, this returns the correct answer
    # (statutorily ineligible) instead of a silently-wrong result set.
    county_entry = COUNTIES[county_id]
    if county_entry.population is not None and county_entry.population > POPULATION_CAP:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{county_entry.name} County (population ~{county_entry.population:,}) "
                f"exceeds the {POPULATION_CAP:,} population cap in "
                f"s. 163.3164(4)(f), F.S. -- agricultural-enclave pathway "
                f"is not available for parcels in this county under SB 686."
            ),
        )

    try:
        rows = scan_orchestrator.run_county_scan(
            county_id=county_id,
            min_acreage=min_acreage,
            max_acreage=max_acreage,
            max_candidates=max_candidates,
            require_single_owner=require_single_owner,
            min_encirclement_pct=min_encirclement_pct,
            flum_character_filter=flum_character,
            surrounding_density_filter=surrounding_density,
        )
    except ImportError as exc:
        # Shapely missing — this is the single most likely first-run
        # failure. Surface it clearly rather than a generic 500.
        raise HTTPException(
            status_code=500,
            detail=f"Missing dependency: {exc}. Run: pip install shapely",
        )
    except Exception as exc:  # noqa: BLE001
        # Exhaustive raw diagnostic: print every possible representation
        # of the exception to rule out any silent truncation, exception
        # chaining, or __str__ override hiding the real message —
        # repeated attempts to add labeled context inside
        # parcel_fetcher.py were not appearing in this endpoint's
        # response despite being confirmed present in the deployed
        # source, so this bypasses all of that and reports the rawest
        # possible view of exactly what Python caught here.
        import traceback
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Scan failed",
                "exception_type": type(exc).__name__,
                "exception_str": str(exc),
                "exception_repr": repr(exc),
                "traceback": tb_str,
            },
        )

    return {
        "county_id": county_id,
        "candidate_count": len(rows),
        "candidates": scan_orchestrator.rows_to_dicts(rows),
    }


@app.get("/api/parcels/{parcel_id}/demographics")
def parcel_demographics(
    parcel_id: str,
    lat: float = Query(..., description="Parcel centroid latitude"),
    lon: float = Query(..., description="Parcel centroid longitude"),
):
    """
    On-demand 5/10/15-mile Census ring demographics for a single parcel.

    Deliberately a separate endpoint from /scan — the frontend should
    only call this when a user explicitly clicks "Pull area demographics"
    on one parcel, never as part of a bulk scan. Requires a Census API
    key (free, see ring_demographics.py header) set as the
    CENSUS_API_KEY environment variable.
    """
    api_key = os.environ.get("CENSUS_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="CENSUS_API_KEY environment variable not set. "
                   "Get a free key at https://api.census.gov/data/key_signup.html",
        )

    try:
        rings = ring_demographics.compute_ring_demographics(
            parcel_centroid_lat=lat,
            parcel_centroid_lon=lon,
            census_api_key=api_key,
        )
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=f"Demographics pull not fully implemented yet: {exc}",
        )
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Missing dependency: {exc}")
    except Exception as exc:  # noqa: BLE001
        # Covers real failure modes confirmed live during implementation:
        # a bad/expired CENSUS_API_KEY (RuntimeError, from an HTML error
        # page instead of JSON) and any requests.RequestException from
        # either the TIGERweb or ACS calls. Same rationale as the /scan
        # endpoint's catch-all: surface the raw exception rather than a
        # bare 500 with no detail.
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Demographics pull failed",
                "exception_type": type(exc).__name__,
                "exception_str": str(exc),
            },
        )

    return {
        "parcel_id": parcel_id,
        "rings": [
            {
                "radius_miles": r.radius_miles,
                "total_population": r.total_population,
                "population_moe": r.population_moe,
                "median_household_income": r.median_household_income,
                "median_age": r.median_age,
                "total_housing_units": r.total_housing_units,
                "density_per_sqmi": r.density_per_sqmi,
            }
            for r in rings
        ],
    }


class DiligenceExportPayload(BaseModel):
    """
    Client sends: raw row data for each selected parcel PLUS its pre-
    computed verification checklist (from buildVerificationChecklist() in
    web/index.html). We do NOT recompute checklist status server-side --
    the export must always match what the UI showed, and duplicating the
    JS logic in Python would silently drift the moment one changes.
    """
    rows: list[dict]


@app.post("/api/export/diligence-tracker")
def export_diligence_tracker(payload: DiligenceExportPayload):
    """
    Generate a styled .xlsx diligence tracker for a client-selected set
    of parcels. See app/diligence_tracker.py for the workbook layout,
    color coding, and freeze-pane details.
    """
    if not payload.rows:
        raise HTTPException(status_code=400, detail="No parcels selected for export.")

    try:
        xlsx_bytes = diligence_tracker.build_diligence_tracker_xlsx(payload.rows)
    except Exception as exc:  # noqa: BLE001
        import traceback
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Failed to build diligence tracker",
                "exception_type": type(exc).__name__,
                "exception_str": str(exc),
                "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            },
        )

    filename = diligence_tracker.build_filename(payload.rows)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =====================================================================
# ZCTA-sectioned coverage tracking (added 2026-07-06 for the v1.5 pass)
# =====================================================================

@app.get("/api/coverage/{county_id}/status")
def coverage_status(county_id: str):
    """
    Return per-ZCTA and county-wide coverage state for the given county.
    Also enumerates every ZCTA that intersects the county boundary so the
    frontend can render a full progress list even before any scans have
    run.

    The county-boundary + ZCTA queries hit Census TIGERweb -- cheap for
    subsequent calls thanks to zcta_client.get_county_zctas's @lru_cache.
    """
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")

    try:
        zctas = zcta_client.get_county_zctas(county_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Could not enumerate ZCTAs for {county_id}: {exc}",
        )

    zcta_codes = [z["zcta5"] for z in zctas]
    ledger_state = coverage_ledger.get_county_state(county_id)
    summary = coverage_ledger.county_summary(county_id, zcta_codes)
    next_zcta = coverage_ledger.next_incomplete_zcta(county_id, zcta_codes)

    per_zcta = []
    for z in zctas:
        entry = ledger_state.get("zctas", {}).get(z["zcta5"], {})
        per_zcta.append({
            "zcta5": z["zcta5"],
            "geoid": z["geoid"],
            "centroid_lat": z["centroid_lat"],
            "centroid_lon": z["centroid_lon"],
            "total_candidates": entry.get("total_candidates"),
            "processed_count": len(entry.get("processed_parcel_ids", [])),
            "complete": bool(entry.get("complete")),
            "last_run_at": entry.get("last_run_at"),
        })

    return {
        "county_id": county_id,
        "summary": summary,
        "next_incomplete_zcta": next_zcta,
        "zctas": per_zcta,
    }


class CoverageAdvancePayload(BaseModel):
    """
    Optional filters -- the same ones the /scan endpoint accepts. The
    coverage flow doesn't need max_candidates (that's max_parcels_per_run
    here) or a county id (that's in the URL path).
    """
    max_parcels_per_run: int = 25
    min_acreage: float = 20.0
    max_acreage: float = 4480.0
    require_single_owner: bool = False
    min_encirclement_pct: Optional[float] = None
    flum_character_filter: Optional[str] = None
    surrounding_density_filter: Optional[str] = None
    # Explicit ZCTA5 to advance -- if omitted, backend picks the next
    # incomplete ZCTA (ascending by ZCTA5).
    zcta5: Optional[str] = None


@app.post("/api/coverage/{county_id}/advance")
def coverage_advance(county_id: str, payload: CoverageAdvancePayload):
    """
    Advance coverage by up to `max_parcels_per_run` parcels within one
    ZCTA of the given county. If `zcta5` is omitted the backend picks
    the next incomplete ZCTA (ascending by ZCTA5 code).

    Idempotent from the client's perspective: parcel_ids already recorded
    in the ledger are skipped, so re-triggering an advance for a ZCTA
    with (say) 400 parcels in 25-parcel batches will visit each parcel
    exactly once across 16 calls, never processing the same one twice.
    """
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")

    county_entry = COUNTIES[county_id]
    if county_entry.population is not None and county_entry.population > POPULATION_CAP:
        raise HTTPException(
            status_code=400,
            detail=f"{county_entry.name} County exceeds the 1.75M population cap in s. 163.3164(4)(f), F.S.",
        )

    try:
        zctas = zcta_client.get_county_zctas(county_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"ZCTA enumeration failed: {exc}")
    zcta_codes = [z["zcta5"] for z in zctas]

    target_zcta5 = payload.zcta5 or coverage_ledger.next_incomplete_zcta(county_id, zcta_codes)
    if target_zcta5 is None:
        return {
            "county_id": county_id,
            "message": "County coverage complete -- every ZCTA has been fully processed.",
            "county_complete": True,
            "processed_this_run": 0,
            "zcta5": None,
            "candidates": [],
            "summary": coverage_ledger.county_summary(county_id, zcta_codes),
        }

    target_zcta = next((z for z in zctas if z["zcta5"] == target_zcta5), None)
    if target_zcta is None:
        raise HTTPException(status_code=400, detail=f"Unknown ZCTA {target_zcta5} for county {county_id}")

    # Establish the total candidate count for this ZCTA (once per ZCTA
    # per process lifetime -- ledger caches it so future advances don't
    # requery). Uses the same ag-use WHERE clause as the fetcher.
    zcta_ledger = coverage_ledger.get_zcta_state(county_id, target_zcta5)
    if zcta_ledger.get("total_candidates") is None:
        from parcel_fetcher import build_ag_where_clause
        try:
            total = zcta_client.count_parcels_in_zcta(
                county_entry.parcel_service_url,
                build_ag_where_clause(county_id),
                target_zcta["geometry"],
            )
            coverage_ledger.set_zcta_total(county_id, target_zcta5, total)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal -- we can still run the pipeline, just without a
            # denominator until the next call.
            pass

    already_processed = set(
        coverage_ledger.get_zcta_state(county_id, target_zcta5).get("processed_parcel_ids", [])
    )

    try:
        rows = scan_orchestrator.run_county_scan(
            county_id=county_id,
            min_acreage=payload.min_acreage,
            max_acreage=payload.max_acreage,
            max_candidates=payload.max_parcels_per_run,
            require_single_owner=payload.require_single_owner,
            min_encirclement_pct=payload.min_encirclement_pct,
            flum_character_filter=payload.flum_character_filter,
            surrounding_density_filter=payload.surrounding_density_filter,
            zcta_geometry=target_zcta["geometry"],
            zcta5=target_zcta5,
            skip_parcel_ids=already_processed,
        )
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Missing dependency: {exc}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        raise HTTPException(status_code=502, detail={
            "message": "Coverage advance failed",
            "exception_type": type(exc).__name__,
            "exception_str": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        })

    processed_ids = [r.parcel_id for r in rows if r.parcel_id]
    coverage_ledger.mark_processed(county_id, target_zcta5, processed_ids)
    # Persist the full row data into the master property database so the
    # ranked view can be built from accumulated history, not just this
    # session. Deliberately part of the same ledger, not a parallel store.
    coverage_ledger.save_parcel_results(county_id, scan_orchestrator.rows_to_dicts(rows))

    summary = coverage_ledger.county_summary(county_id, zcta_codes)
    zcta_state = coverage_ledger.get_zcta_state(county_id, target_zcta5)

    return {
        "county_id": county_id,
        "zcta5": target_zcta5,
        "processed_this_run": len(processed_ids),
        "candidates": scan_orchestrator.rows_to_dicts(rows),
        "zcta_state": {
            "total_candidates": zcta_state.get("total_candidates"),
            "processed_count": len(zcta_state.get("processed_parcel_ids", [])),
            "complete": bool(zcta_state.get("complete")),
        },
        "summary": summary,
        "county_complete": summary["county_complete"],
        "next_incomplete_zcta": coverage_ledger.next_incomplete_zcta(county_id, zcta_codes),
    }


# Sort order: real candidates first, excluded at the very bottom, in
# the exact order Tyler specified 2026-07-06.
_TIER_SORT_ORDER = {
    "confirmed_qualifying": 0,
    "strong_candidate":     1,
    "watch_list":           2,
    "unlikely":             3,
    "excluded":             4,
    # Legacy old-tier fallback so pre-migration rows still sort sensibly.
    "confident": 0,
    "possible":  1,
    "watch":     2,
}


@app.get("/api/property-db/{county_id}/ranked")
def property_db_ranked(county_id: str):
    """
    Return every parcel ever scanned for this county, sorted by master
    tier first (confirmed -> strong -> watch -> unlikely -> excluded)
    and attractiveness score descending within tier.

    This is the persistent, ranked view Tyler wants the UI to present
    as the primary way of looking at scan results -- not per-session,
    but accumulated across every run over time via the coverage_ledger's
    master property database.
    """
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")
    rows = coverage_ledger.list_all_parcels(county_id)
    rows.sort(key=lambda r: (
        _TIER_SORT_ORDER.get(r.get("tier") or r.get("confidence_tier") or "unlikely", 3),
        -(r.get("attractiveness_score") or 0),
    ))
    return {
        "county_id": county_id,
        "total": len(rows),
        "tier_distribution": coverage_ledger.tier_distribution(county_id),
        "parcels": rows,
    }


@app.post("/api/coverage/{county_id}/reset")
def coverage_reset(county_id: str):
    """Wipe the coverage ledger for one county (start fresh)."""
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")
    coverage_ledger.reset_county(county_id)
    return {"county_id": county_id, "reset": True}
