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

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))

from county_registry import COUNTIES, POPULATION_CAP  # noqa: E402
import scan_orchestrator  # noqa: E402
import ring_demographics  # noqa: E402
import diligence_tracker  # noqa: E402
import coverage_ledger  # noqa: E402
import zcta_client  # noqa: E402
import background_jobs  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from dataclasses import asdict  # noqa: E402


# At startup, flip any job whose process died mid-run from "running"
# -> "interrupted" so the frontend can offer a resume rather than
# showing stale progress. Safe to call every restart; a no-op when
# no interrupted jobs exist.
background_jobs.mark_interrupted_at_startup()


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
    """Liveness check — Render/Railway ping this to confirm the service is up.
    Also surfaces the resolved data_dir so a caller can eyeball whether the
    persistent-disk mount actually took effect (roadmap item 12)."""
    return {
        "status": "ok",
        "code_version": _resolve_code_version(),
        "data_dir": coverage_ledger._LEDGER_DIR,
    }


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
    debug: int = Query(0, description="If 1, include per-block-group raw ACS values for hand-verification."),
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
        # Always populate block_group_detail internally -- the trend
        # calculation below needs the 15-mile GEOID list. If debug=0,
        # we strip block_group_detail from the response payload before
        # returning so we don't bloat the on-the-wire size.
        rings, containing_bg_geoid = ring_demographics.compute_ring_demographics(
            parcel_centroid_lat=lat,
            parcel_centroid_lon=lon,
            census_api_key=api_key,
            include_block_group_detail=True,
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

    # Phase D: population trend. Uses the county the parcel sits in
    # (from any included BG's 12-digit GEOID) for the reliable county
    # trend, and the 15-mile ring's block groups for the directional
    # ring trend. Trend fetch failures are non-fatal -- the trend
    # response fields go None with a note.
    trend_response = None
    try:
        largest_ring = max(rings, key=lambda r: r.radius_miles)
        largest_geoids = [
            bg["geoid"] for bg in (largest_ring.block_group_detail or [])
            if bg.get("geoid") and len(bg["geoid"]) == 12
        ]
        # FIX (2026-07-06): use the FIPS from the BG that actually
        # contains the parcel centroid, not the first BG in the ring
        # (which could be in an adjacent county when the ring straddles
        # a county boundary -- e.g. a Pasco parcel with a 15-mile ring
        # picks up Hernando/Hillsborough/Sumter BGs, and "first" was
        # arbitrary).
        state_fips = None
        county_fips = None
        if containing_bg_geoid and len(containing_bg_geoid) == 12:
            state_fips = containing_bg_geoid[:2]
            county_fips = containing_bg_geoid[2:5]
        if state_fips and county_fips:
            trend = ring_demographics.fetch_population_trend(
                state_fips=state_fips,
                county_fips=county_fips,
                ring_block_group_geoids=largest_geoids,
                ring_current_population=int(largest_ring.total_population or 0),
                census_api_key=api_key,
            )
            trend_response = asdict(trend)
    except Exception as exc:  # noqa: BLE001 -- trend is optional context, don't sink the main response
        trend_response = {"error": f"Trend fetch failed: {type(exc).__name__}: {exc}"}

    return {
        "parcel_id": parcel_id,
        "rings": [
            {
                "radius_miles": r.radius_miles,
                "total_population": r.total_population,
                "population_moe": r.population_moe,
                "median_household_income": r.median_household_income,
                "income_moe": r.income_moe,
                "median_age": r.median_age,
                "total_housing_units": r.total_housing_units,
                "density_per_sqmi": r.density_per_sqmi,
                "block_groups_included": r.block_groups_included,
                # Phase C metrics
                "median_home_value": r.median_home_value,
                "home_value_moe": r.home_value_moe,
                "median_gross_rent": r.median_gross_rent,
                "gross_rent_moe": r.gross_rent_moe,
                "rent_burden_pct": r.rent_burden_pct,
                "homeownership_rate_pct": r.homeownership_rate_pct,
                "renter_occupied_pct": r.renter_occupied_pct,
                "avg_household_size": r.avg_household_size,
                "family_household_pct": r.family_household_pct,
                "income_distribution": r.income_distribution,
                "age_distribution": r.age_distribution,
                # Debug detail: only surfaced to the wire when the
                # caller opts in via ?debug=1.
                "block_group_detail": r.block_group_detail if bool(debug) else None,
            }
            for r in rings
        ],
        "trend": trend_response,
    }


def _require_debug_key(header_key: Optional[str], query_key: Optional[str]) -> None:
    """
    Shared-secret gate for /api/debug/* endpoints. Fails CLOSED: if
    DEBUG_API_KEY isn't configured on the server at all, the endpoint
    refuses every request rather than silently going public. Accepts
    the key via the X-Debug-Key header (preferred) or a ?debug_key=
    query param (fallback for quick curl tests).
    """
    import hmac
    expected = os.environ.get("DEBUG_API_KEY")
    if not expected:
        # Fail closed. This is deliberate -- a missing env var must not
        # accidentally re-expose the endpoint (roadmap item 1, 2026-07).
        raise HTTPException(
            status_code=503,
            detail="Debug endpoints disabled: DEBUG_API_KEY not configured",
        )
    provided = header_key or query_key
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing debug key")


@app.get("/api/debug/acs-probe")
def acs_probe(
    variables: str = Query(..., description="Comma-separated ACS variable codes to probe, e.g. B25077_001E,B25008_001E"),
    state: str = Query("12", description="Two-digit FIPS, default 12=Florida"),
    county: str = Query("101", description="Three-digit FIPS, default 101=Pasco"),
    tract: str = Query("030901", description="Six-digit tract code, default one in Pasco"),
    block_group: str = Query("*", description="Block group number (default * = all)"),
    year: int = Query(2023, description="ACS 5-year vintage (endpoint year). Used for pop-trend probes."),
    # Optional geography overrides. When set, they replace the
    # block-group-scoped defaults above so this endpoint can probe any
    # Census geography level (place, county, state, etc.) without a
    # separate endpoint per level. Consistent with acs-probe's original
    # "ad-hoc probe" intent. Roadmap item 5 (metro proximity) is the
    # first caller that needs place-level data.
    for_clause: Optional[str] = Query(None, description="Raw ACS `for=` clause (e.g. 'place:*'). Overrides block_group defaults."),
    in_clause: Optional[str] = Query(None, description="Raw ACS `in=` clause (e.g. 'state:12'). Overrides the state+county+tract defaults."),
    debug_key: Optional[str] = Query(None, description="Shared secret (fallback to X-Debug-Key header)"),
    x_debug_key: Optional[str] = Header(None, description="Shared secret matching DEBUG_API_KEY env var"),
):
    """
    Ad-hoc ACS variable probe for Phase B (2026-07-06 pass): confirms
    exact variable codes, availability at block-group vs. tract level,
    and margin-of-error suffixes for new metrics we're adding to the
    demographics pull. Renders a raw response so a caller can eyeball
    it before wiring the variable in. Deliberately unscoped to any
    parcel -- just proves the code returns data.

    Gated behind DEBUG_API_KEY (roadmap item 1) -- fails closed if that
    env var is unset. Pass the secret via the X-Debug-Key header or a
    ?debug_key= query param.
    """
    _require_debug_key(x_debug_key, debug_key)
    api_key = os.environ.get("CENSUS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="CENSUS_API_KEY not set")
    import requests
    from urllib.parse import quote_plus
    var_list = ",".join(v.strip() for v in variables.split(",") if v.strip())
    base = f"https://api.census.gov/data/{year}/acs/acs5"
    for_part = quote_plus(for_clause) if for_clause else f"block%20group:{block_group}"
    in_part = quote_plus(in_clause) if in_clause else f"state:{state}+county:{county}+tract:{tract}"
    url = f"{base}?get=NAME,{var_list}&for={for_part}"
    if in_part:  # some geography levels (e.g. state:*) have no `in=` clause
        url += f"&in={in_part}"
    url += f"&key={api_key}"
    try:
        resp = requests.get(url, timeout=30)
        # Preserve non-JSON responses (e.g. the "Invalid Key" HTML page)
        # so the caller sees exactly what the ACS API returned.
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw_text_start": resp.text[:400]}
        return {
            "url": url.replace(api_key, "***"),
            "status": resp.status_code,
            "payload": payload,
        }
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/debug/metro-verify")
def metro_verify(
    county_id: Optional[str] = Query(None, description="Restrict to one county's parcels (default = all counties)"),
    max_parcels: int = Query(20, description="Cap on parcels scored per request"),
    max_miles: float = Query(50.0, description="Only consider FL places within this radius"),
    debug_key: Optional[str] = Query(None),
    x_debug_key: Optional[str] = Header(None),
):
    """
    Verification harness for roadmap item 5 (metro proximity). Loads FL
    Census places (~800), then scores a batch of already-scanned real
    parcels from the coverage ledger against them, returning the raw
    inputs alongside the computed metro_pull_score so the formula can
    be sanity-checked before wiring this into the parcel pipeline
    broadly.

    Ad-hoc; gated by DEBUG_API_KEY like /api/debug/acs-probe.
    """
    _require_debug_key(x_debug_key, debug_key)
    census_api_key = os.environ.get("CENSUS_API_KEY")
    if not census_api_key:
        raise HTTPException(status_code=500, detail="CENSUS_API_KEY not set")
    import metro_proximity
    try:
        places = metro_proximity.fetch_fl_places(census_api_key)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"FL places fetch failed: {exc}")

    # Pull real parcels from the master DB. County-scoped if requested,
    # otherwise round-robin across every county so a "no county_id" call
    # gives representative coverage.
    if county_id:
        parcels = coverage_ledger.list_all_parcels(county_id)
    else:
        parcels = coverage_ledger.list_all_parcels_all_counties()
    parcels = [p for p in parcels if p.get("centroid_lat") is not None][:max_parcels]

    results = []
    for p in parcels:
        mp = metro_proximity.metro_proximity_for_parcel(
            p["centroid_lat"], p["centroid_lon"], places, max_miles=max_miles,
        )
        results.append({
            "parcel_id": p.get("parcel_id"),
            "county_id": p.get("county_id"),
            "tier": p.get("tier") or p.get("confidence_tier"),
            "acreage": p.get("acreage"),
            "centroid_lat": p.get("centroid_lat"),
            "centroid_lon": p.get("centroid_lon"),
            "metro_proximity": asdict(mp) if mp else None,
        })

    # Simple spot-check: 3 places closest to each pilot-county seat, so
    # the caller can eyeball "does Tampa show up near Pasco parcels."
    reference_points = {
        "pasco (28.30, -82.42)": (28.30, -82.42),
        "nassau (30.62, -81.71)": (30.62, -81.71),
        "st_johns (29.90, -81.34)": (29.90, -81.34),
        "osceola (28.29, -81.41)": (28.29, -81.41),
    }
    reference_hits = {}
    for label, (lat, lon) in reference_points.items():
        top = metro_proximity.nearest_places(lat, lon, places, max_miles=max_miles, max_results=3)
        reference_hits[label] = [
            {
                "name": p.basename,
                "distance_mi": round(d, 2),
                "population": p.population,
                "median_hh_income": p.median_household_income,
                "metro_pull_score": metro_proximity.compute_metro_pull(
                    p.population, p.median_household_income, d,
                ),
            }
            for p, d in top
        ]

    return {
        "fl_places_loaded": len(places),
        "reference_nearest_places_by_pilot_county": reference_hits,
        "parcels_scored": len(results),
        "results": results,
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
    # requery). Roadmap item 11 (2026-07-06): uses
    # parcel_fetcher.count_matching_candidates, NOT the older
    # zcta_client.count_parcels_in_zcta. The old function counted every
    # parcel matching the server-side ag WHERE + ZCTA intersect, but the
    # actual fetcher then applied client-side acreage bounds + is_agricultural
    # re-check, silently dropping many. Any parcel matching the WHERE
    # but failing a client-side filter inflated total_candidates without
    # ever being fetchable, so the ledger's "remaining" number never
    # reached zero and the job runner terminated with "0 rows but N
    # candidates remaining." Audit across 12 sampled ZCTAs found 52% of
    # the old total was spurious. count_matching_candidates uses the same
    # code path as the fetcher, guaranteeing count == fetchable by
    # construction.
    zcta_ledger = coverage_ledger.get_zcta_state(county_id, target_zcta5)
    from parcel_fetcher import count_matching_candidates
    if zcta_ledger.get("total_candidates") is None:
        try:
            total = count_matching_candidates(
                county_id=county_id,
                zcta_geometry=target_zcta["geometry"],
                min_acreage=payload.min_acreage,
                max_acreage=payload.max_acreage,
                require_single_owner=payload.require_single_owner,
            )
            coverage_ledger.set_zcta_total(county_id, target_zcta5, total)
        except Exception:  # noqa: BLE001
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

    # Self-heal for ledgers whose total_candidates was computed by the
    # OLD (pre-item-11) counter. If this advance returned 0 rows but the
    # ledger still says >0 remain, re-verify the total via the same
    # code path as the fetcher. If the recomputed total <= processed
    # count, the old total was inflated -- update it and let mark_processed
    # flip the completeness flag. Guards against a background job hitting
    # the same "0 rows / N remaining" error state on ledger state
    # persisted before the fix. Only fires when there's a real
    # divergence to fix.
    zcta_state_pre_heal = coverage_ledger.get_zcta_state(county_id, target_zcta5)
    stored_total = zcta_state_pre_heal.get("total_candidates")
    stored_processed = len(zcta_state_pre_heal.get("processed_parcel_ids", []))
    if (not rows) and stored_total is not None and stored_processed < stored_total:
        try:
            reverified = count_matching_candidates(
                county_id=county_id,
                zcta_geometry=target_zcta["geometry"],
                min_acreage=payload.min_acreage,
                max_acreage=payload.max_acreage,
                require_single_owner=payload.require_single_owner,
            )
            if reverified != stored_total:
                coverage_ledger.set_zcta_total(county_id, target_zcta5, reverified)
        except Exception:  # noqa: BLE001 -- self-heal is best-effort
            pass

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


@app.get("/api/property-db/all")
def property_db_all():
    """
    Every parcel ever scanned across every county, unsorted -- for the
    master-database map view. Callers filter and color client-side
    (tier/pathway/county/coverage-status). Each parcel row includes
    `geometry_wgs84` (WGS84 lat/lon polygon coords) when available so
    Leaflet can render real shapes, not just centroid pins.
    """
    parcels = coverage_ledger.list_all_parcels_all_counties()
    tier_totals: dict[str, int] = {}
    for r in parcels:
        t = r.get("tier") or r.get("confidence_tier") or "unlikely"
        tier_totals[t] = tier_totals.get(t, 0) + 1
    return {
        "total": len(parcels),
        "tier_distribution": tier_totals,
        "parcels": parcels,
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


# =====================================================================
# Full-county background scan (added 2026-07-06). Daemon-thread job
# runner, JSON-backed state, polling-driven progress in the UI. See
# app/background_jobs.py for the architecture write-up.
# =====================================================================

class FullCountyScanPayload(BaseModel):
    """Same scan filters as the manual /advance endpoint; the runner
    threads them through each per-ZCTA batch."""
    max_parcels_per_run: int = 25
    min_acreage: float = 20.0
    max_acreage: float = 4480.0
    require_single_owner: bool = False
    min_encirclement_pct: Optional[float] = None
    flum_character_filter: Optional[str] = None
    surrounding_density_filter: Optional[str] = None


@app.post("/api/coverage/{county_id}/scan-entire-county")
def scan_entire_county(county_id: str, payload: FullCountyScanPayload):
    """
    Kick off (or return the already-running) full-county scan job.
    Idempotent: a second POST with a job in flight returns that job's
    current state instead of spawning a duplicate.
    """
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")
    county_entry = COUNTIES[county_id]
    if county_entry.population is not None and county_entry.population > POPULATION_CAP:
        raise HTTPException(
            status_code=400,
            detail=f"{county_entry.name} County exceeds the {POPULATION_CAP:,} population cap.",
        )
    state = background_jobs.start_full_county_job(county_id, payload.model_dump())
    return asdict(state)


@app.get("/api/coverage/{county_id}/job-status")
def coverage_job_status(county_id: str):
    """Return the current background-job state for this county, or None."""
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")
    state = background_jobs.get_state(county_id)
    return {"county_id": county_id, "job": asdict(state) if state else None}


@app.post("/api/coverage/{county_id}/job-cancel")
def coverage_job_cancel(county_id: str):
    """Signal the background job to stop between batches (safe cancel)."""
    if county_id not in COUNTIES:
        raise HTTPException(status_code=404, detail=f"Unknown county: {county_id}")
    state = background_jobs.cancel_job(county_id)
    return {"county_id": county_id, "job": asdict(state) if state else None}
