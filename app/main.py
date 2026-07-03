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

from county_registry import COUNTIES  # noqa: E402
import scan_orchestrator  # noqa: E402
import ring_demographics  # noqa: E402


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
    notes: str


class ScanRequest(BaseModel):
    min_acreage: float = 20.0
    max_acreage: float = 1280.0
    require_single_owner: bool = True


@app.get("/health")
def health():
    """Liveness check — Render/Railway ping this to confirm the service is up."""
    return {"status": "ok"}


@app.get("/api/counties", response_model=list[CountyInfo])
def list_counties():
    """
    Return all counties in the registry with their resolution status.
    'live' here reflects whether county_registry.py has a confirmed
    flu_field — NOT whether this endpoint has actually test-queried it.
    Treat 'live' as "worth trying," not "guaranteed to work," until each
    has been exercised against a real request at least once.
    """
    result = []
    for county_id, county in COUNTIES.items():
        is_live = county.flu_field not in ("UNKNOWN", "LU_DESC", "FLUNAME") or county_id in (
            "hillsborough", "orange", "pasco", "brevard", "volusia",
        )
        # NOTE: the flu_field-based live check above is a rough heuristic
        # carried over from the registry's own inline comments — Sarasota's
        # LU_DESC and Manatee's FLUNAME are themselves best-guess field
        # names pending confirmation, not confirmed-live like the other
        # five. Consider this endpoint's "live" flag provisional for those
        # two until describe_layer() is run against them for real.
        result.append(CountyInfo(
            id=county.id,
            name=county.name,
            fips=county.fips,
            flum_service_url=county.flum_service_url,
            flu_field=county.flu_field,
            live=is_live,
            notes=county.notes,
        ))
    return result


@app.get("/api/counties/{county_id}/scan")
def scan_county(
    county_id: str,
    min_acreage: float = Query(20.0, ge=0),
    max_acreage: float = Query(1280.0, gt=0),
    max_candidates: int = Query(25, ge=1, le=200, description="Caps how many parcels get the full (slower) encirclement check. Start small (10-25) for testing."),
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

    try:
        rows = scan_orchestrator.run_county_scan(
            county_id=county_id,
            min_acreage=min_acreage,
            max_acreage=max_acreage,
            max_candidates=max_candidates,
        )
    except ImportError as exc:
        # Shapely missing — this is the single most likely first-run
        # failure. Surface it clearly rather than a generic 500.
        raise HTTPException(
            status_code=500,
            detail=f"Missing dependency: {exc}. Run: pip install shapely",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Scan failed: {exc}")

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
