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


@app.get("/health")
def health():
    """Liveness check — Render/Railway ping this to confirm the service is up."""
    return {"status": "ok", "code_version": "step-labels-v3-2026-07-03"}


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
