# Status — 2026-07-03 (updated: blockers 1 and 2 resolved)

Ground-truthing pass against live data for the FL agricultural enclave
scanner (SB 686 / HB 691, F.S. 163.3164(4)). This file is the handoff
point if context resets — read this before re-deriving anything.

## Confirmed working (tested against live endpoints, not assumed)

- **Statewide cadastral layer (`STATEWIDE_CADASTRAL_URL`)**: live, real
  fields `CO_NO`/`DOR_UC` confirmed. `DOR_UC` range fix (`'050'`,`'069'`)
  is correct — real agricultural parcels returned. **But filtering this
  layer by `CO_NO` is broken** (see Blocker 3, now the reason we pivoted
  away from it — not an open blocker itself, just background).

- **Per-county parcel layers — the new primary data source**, all four
  target counties confirmed live via `describe_layer` (`?f=pjson`) +
  test queries, real field names in `county_registry.py`:
  - **Pasco**: `mapping.pascopa.com/.../Parcels/MapServer/3`, use-code
    field `DIR_CLASS`, acreage `VAL_ACRES`, owner `NAD_NAME_1`/`NAD_NAME_2`.
  - **Nassau**: `services2.arcgis.com/.../Parcels_in_Baker_and_Nassau_Counties/FeatureServer/0`,
    real statewide `DORUC` field, acreage `ACRES`, owner `ONAME`. Shared
    with Baker County — needs `CNTYNAME='NASSAU'` filter (confirmed
    uppercase in real data).
  - **St. Johns**: `www.gis.sjcfl.us/portal_sjcgis/.../Parcel/MapServer/0`
    (note: bare `gis.sjcfl.us` does not resolve — must be `www.` + the
    `/portal_sjcgis/` path). Use-code field `USE_CODE` is a 4-char
    **county-local** code, not statewide DOR_UC. No acreage field at all
    (`Shape_STArea__` always 0.0) — acreage computed from geometry.
  - **Osceola**: `gis.osceola.org/hosting/.../Parcels/FeatureServer/3`.
    Use-code field `DORCode` is also county-local despite the name.
    Acreage `TotalAcres`. Has a clean `Jurisdiction` field
    (`Unincorporated`/`incorporated`+city) — best-supported county for
    the still-unimplemented unincorporated hard filter.

- **Acreage fields verified as real acres** (not sqft or another unit):
  cross-checked each against an independently shoelace-computed polygon
  area (geometry fetched with `outSR=3086`, Florida Albers equal-area):
  - Pasco `VAL_ACRES` 18.85 vs. computed 18.82 ✓
  - Osceola `TotalAcres` 48.96 vs. computed 48.94 ✓
  - Nassau `ACRES` 646.341 vs. computed 646.34 ✓ (this one needed a fix
    to `polygon_area_acres()` — the first verification attempt only
    summed the polygon's first ring; this parcel has 2 rings, and Esri's
    format requires a **signed** sum across every ring, not `abs()` of
    just the first, to handle multi-part parcels correctly)

- **St. Johns geometry reprojection**: confirmed live that its parcel
  layer's native SR is Web Mercator (wkid 3857/102100) — projected, but
  NOT equal-area. Computing area directly in it overstated a real
  parcel's acreage by a measured 1.338x, matching the theoretical
  distortion of 1/cos²(30°) = 1.333 at this layer's latitude. Fix:
  `parcel_fetcher.py` requests `outSR=3086` on every geometry fetch,
  regardless of a layer's native SR, so area math is always done in an
  equal-area projection. `AREA_SR = 3086` constant in that file.

- **Per-county agricultural classification** (`app/parcel_fetcher.py`,
  `_AG_CLASSIFIERS` dict) — four separate functions, not one shared
  filter, because the comparison logic genuinely differs:
  - Pasco (`DIR_CLASS`) / Nassau (`DORUC`): string range comparison.
  - St. Johns (`USE_CODE`) / Osceola (`DORCode`): explicit code lists,
    NOT a range — a naive range on Osceola's `DORCode` was confirmed
    live to silently match `'0611'` ("RETIREMENT HOMES") because that
    string sorts lexically between `'050'` and `'069'` despite being an
    unrelated code. Osceola's WHERE clause uses
    `CAST(DORCode AS INTEGER) IN (...)`; the client-side classifier also
    casts to int before comparing.
  - St. Johns' `'9900'` ("Acreage Not Zoned Agricultural") is explicitly
    excluded despite the misleading name.
  - 18/18 unit tests pass (`is_agricultural()` against known real and
    known-bad codes, including both traps above). Live end-to-end
    `fetch_candidate_parcels()` runs succeeded for **Pasco, Nassau, and
    St. Johns** with real owners/codes/acreage. Osceola blocked — see
    Blocker 2 below.

- **`app/parcel_fetcher.py` and `app/scan_orchestrator.py` rewritten**
  to use each county's own parcel layer (`CountyEndpoint.parcel_*`
  fields in `county_registry.py`) instead of the old
  statewide-cadastral-filtered-by-`CO_NO` approach. Single-pass fetch
  (WHERE + geometry together) replaces the old two-pass
  attrs-then-geometry/OBJECTID-batch design — that complexity existed
  specifically to survive the 10.8M-row statewide layer; these
  county-scoped tables are far smaller and confirmed fast even with
  geometry included.

## Blockers 1 and 2 — RESOLVED 2026-07-03

### Blocker 1: `shapely` fails to build on this machine — RESOLVED
Root cause confirmed: Python 3.14 (the only version the `py` launcher
had registered) has no prebuilt wheel for `shapely==2.0.7`, forcing a
source build that needs GEOS headers not installed on this machine.
Rather than chasing a cp314 wheel, installed Python 3.12.10 via
`winget install Python.Python.3.12` and created a project-local venv:
`.venv312/` at the repo root. `pip install -r requirements.txt` there
pulls a prebuilt `shapely-2.0.7-cp312-cp312-win_amd64.whl` — no source
build, no GEOS headers needed. Confirmed both `import shapely` (basic
polygon `.area`) and `import encirclement` (the actual module blocked
by this) work cleanly in `.venv312`. Use `.venv312/Scripts/python` for
all local dev/testing from now on instead of the system `py -3.14`.

Added `certifi>=2024.0` to `requirements.txt` as a direct (previously
only transitive) dependency — needed for Blocker 2's fix below.

### Blocker 2: Osceola's server has a broken TLS certificate chain — RESOLVED
Fetched the missing intermediate ("Entrust DV TLS Issuing RSA CA 2")
from the leaf cert's own AIA "CA Issuers" URL
(`http://crt.sectigo.com/EntrustDVTLSIssuingRSACA2.crt`), converted
DER→PEM, and committed it at
`app/certs/entrust_dv_tls_issuing_rsa_ca_2.pem`. `app/arcgis_client.py`
now builds a combined CA bundle (certifi's default bundle + this one
extra cert, cached per-process via `functools.lru_cache` to a temp
file) and passes it as `verify=` specifically for requests to
`gis.osceola.org` (`_HOST_EXTRA_INTERMEDIATES` dict, keyed by hostname,
checked in `_verify_for_url()`); every other host keeps default
`verify=True`. This is real chain validation, not a `verify=False`
downgrade. Confirmed live: `describe_layer()` and `query_layer_count()`
against Osceola's parcel layer both succeed now with no SSL error.

**Bug found and fixed while verifying the above**: once TLS was no
longer the blocker, `fetch_candidate_parcels("osceola")` failed with a
live ArcGIS 400 "Unable to complete operation" — caused by
`parcel_fetcher.py` using `CountyEndpoint.jurisdiction_field`
("Jurisdiction") in the PARCEL layer's `outFields`, but that field name
belongs to Osceola's separate FLUM layer; the parcel layer's real field
is `Jurisdicti` (per this file's own pre-existing note, previously
never actually exercised because TLS blocked every Osceola parcel
call). Fixed by adding a distinct `parcel_jurisdiction_field` to
`CountyEndpoint` (`county_registry.py`), set to `"Jurisdicti"` for
Osceola and `None` elsewhere (no other county's parcel layer has a
jurisdiction field), and switching `parcel_fetcher.py` to read that
instead of the FLUM-layer field. All four counties now confirmed live
end-to-end via `fetch_candidate_parcels()`, including Osceola (real
parcels returned, e.g. Walt Disney Parks and Resorts US Inc, Farmland
Reserve Inc).

**New data gap found, not a code bug**: Osceola's parcel-layer
`Jurisdicti`/`JurisDesc` fields exist and are queryable (the 400 is
gone) but are NULL on every sampled row — so the previously-noted
"best-supported unincorporated filter" data does NOT actually exist at
the parcel-layer level after all. The FLUM layer's `Jurisdiction` field
(a separate service, confirmed populated with real values earlier) is
unaffected by this — the unincorporated hard filter would need to join
against the FLUM layer, not rely on the parcel layer's own jurisdiction
fields as previously assumed.

## `run_county_scan()` run end-to-end — 2026-07-03, three real bugs found and fixed

Ran the full pipeline (`fetch_candidate_parcels` → `encirclement` →
`exclusions` → `scoring`) live via `.venv312` for all four counties, not
just Pasco. No exceptions anywhere, but the FIRST run silently returned
`pct_perimeter_qualifying = 0.0` for every single Pasco candidate
regardless of real acreage/location — a red flag, not a real result
(confirmed real variation exists once fixed: see below). Root-caused
three separate, real bugs, all now fixed:

1. **`_buffer_esri_geometry` asserted the wrong spatial reference**
   (`scan_orchestrator.py`). It read `geometry.get("spatialReference")`
   with a fallback default of wkid 2236 — but ArcGIS Server does NOT
   include a `spatialReference` on each feature's geometry in a
   `/query` response (only once, at the FeatureSet root, which
   `arcgis_client.query_layer` discards), so the fallback always fired.
   Every candidate geometry is actually in `AREA_SR` (3086, meters) —
   asserting 2236 (State Plane feet) mislabeled the coordinates sent to
   the FLUM layer's spatial filter, so the query silently matched zero
   real neighbors. Fixed: hardcode `{"wkid": AREA_SR}` instead of
   reading a field that's never actually present. Also fixed a related
   unit bug this exposed: the buffer distance parameter is named/passed
   in feet, but was being applied directly to coordinates now correctly
   known to be in meters — added an explicit feet→meters conversion
   before calling Shapely's `.buffer()`.

2. **`query_layer` call for FLUM neighbors didn't request `out_sr`**
   (`scan_orchestrator.py`). Once bug 1 was fixed, real neighbors
   started coming back — but in the FLUM layer's own native SR (Web
   Mercator/3857 for Pasco, confirmed via `describe_layer`), not 3086,
   so `compute_encirclement`'s Shapely intersection was comparing two
   geometries in incompatible coordinate systems. Fixed by passing
   `out_sr=AREA_SR` on that query, same as every other geometry fetch in
   this project.

3. **`esri_json_to_shapely` mishandled real multipart FLUM polygons**
   (`encirclement.py`) — this was already flagged as a known gap in its
   own docstring, now hit live. It assumed ring 0 is always the sole
   exterior and every other ring is a hole; Pasco's FLUM layer has
   genuinely multipart geometries (multiple real exterior rings, not
   holes), and the naive version produced an INVALID, self-intersecting
   Shapely polygon whose intersection with the real candidate geometry
   came back nonsensical (confirmed live: intersection area equaled the
   candidate's own full area, boundary-to-boundary length came back
   0.0). Fixed by classifying each ring as exterior vs. hole by winding
   direction (Esri's actual convention — clockwise/negative signed
   shoelace area = exterior, counter-clockwise/positive = hole) and
   assigning each hole to whichever exterior ring actually contains it,
   returning a `MultiPolygon` when there's more than one real part.
   `_buffer_esri_geometry` was updated to handle a `MultiPolygon` result
   (build one Esri ring per part) instead of assuming `.exterior`
   always exists.

**Fourth issue found, algorithmic not a data bug**: even after fixing
1–3, `compute_encirclement` still returned 0% for a candidate with a
real, valid, correctly-classified qualifying (`RES-6`) neighbor. Cause:
it measured `boundary.intersection(neighbor_poly.boundary)` — a
boundary-to-boundary LINE intersection, which requires the candidate's
edge to exactly coincide with the FLUM polygon's edge. Confirmed live
this is essentially never true: FLUM designations are a land-use
overlay, not a parcel-boundary layer, and routinely CONTAIN a candidate
parcel entirely (the ground a currently-agricultural parcel sits on can
already carry a future-residential FLUM designation) rather than merely
bordering it — in that case the neighbor polygon's boundary never
touches the candidate's edge at all, even though the correct real-world
answer is "100% encircled." Fixed by measuring
`boundary.intersection(neighbor_poly)` instead (candidate boundary LINE
intersected with neighbor AREA, not neighbor boundary) — this correctly
handles both a parcel sitting inside one big qualifying zone (full
perimeter counts) and a parcel merely bordering a smaller adjacent zone
(only the touching stretch counts). Added a defensive cap so summed
segment lengths can never nonsensically exceed 100% of the real
perimeter.

**Confirmed correct after all four fixes**: ran all four counties
end-to-end again. Pasco and Osceola candidates now show real,
differentiated results (e.g. one Pasco parcel at exactly 100% qualifying
→ pathway 1 matched; others at 3–32%, no pathway). Nassau and St. Johns
still correctly show 0% for their sampled candidates (large remote
Rayonier timberland tracts) — verified this is a REAL result, not the
same bug: their one real neighbor per parcel is genuinely
`'Agriculture'` (Nassau) / `'RUR/SYLV'` (St. Johns), both correctly
non-qualifying, not an artifact of zero-neighbors-found.

**Fifth issue, a real data-correction, not a code bug**: while
debugging, discovered Pasco's `CountyEndpoint.flu_field`/
`agricultural_flu_values` (`county_registry.py`) — already flagged as
an UNCONFIRMED CARRYOVER guess (`"COMP_LAND_"` / `"Agricultural/Rural"`)
— don't exist on the real layer at all. Confirmed via live
`describe_layer` + a full distinct-values query (48 combinations, 1476
features): the real field is `FLU_CODE`, and the real agricultural
codes are `'AG'` and `'AG/R'`. Fixed in `county_registry.py`. Before
this fix, every Pasco encirclement check was silently comparing against
a field that always returned `None` — this compounded with bugs 1–4
above to make the very first live run fully invisible as a bug (0% for
every candidate looked plausible on its own).

Centroid computation (`get_centroid_lat_lon`, hand-written Albers
inverse) was also exercised live for the first time this pass and
checked out — Pasco/Osceola parcels came back at real, correct-looking
lat/lon (e.g. 28.41°N/-82.66°W for a Pasco parcel, genuinely within
Pasco County).

`exclusions.py` and `scoring.py` ran without incident — both are pure
Python with no live-data assumptions, and produced sensible output
(manual-review flags, 0–100 scores with visible breakdowns) on the
first try.

## Step 5 — dashboard wired to real backend, 2026-07-03

`web/index.html` previously ran its own client-side scan directly against
the statewide cadastral layer with Turf.js — completely bypassing every
fix above, including all five encirclement bugs and the Pasco `flu_field`
correction. Rewrote it to call the existing FastAPI backend
(`app/main.py`) instead: `/api/counties` populates the county list live
(no more hardcoded `CO_NO` list), `/api/counties/{id}/scan` runs the real
pipeline. API base URL is a configurable field (persisted to
localStorage), defaulting to `http://localhost:8000` for local dev.
County cards now show "confirmed live end-to-end" only for
pasco/nassau/st_johns/osceola (hardcoded `CONFIRMED_LIVE` set in the
frontend, since `/api/counties`'s own `live` flag is a rough heuristic
that also marks hillsborough/orange/brevard/volusia true despite those
never having been ground-truthed this pass — see that endpoint's own
docstring). Table columns and CSV export now match the real
`ScanResultRow` field names (`pct_perimeter_qualifying`,
`likely_pathways`, `attractiveness_score`, `exclusion_flags`,
`needs_manual_review`) instead of the old mock shape.

Confirmed working end-to-end locally: started `uvicorn app.main:app` via
`.venv312`, loaded `web/index.html` in a browser, ran a live scan against
Pasco through the UI — returned the same real result already documented
above (parcel `35-24-16-0000-00100-0011`, 100% qualifying perimeter,
pathway 1), this time via the actual HTTP path the deployed frontend will
use, not a direct Python call.

Not done as part of this pass: deploying the backend anywhere with real
internet access (`DEPLOYMENT.md` Step 1) or the frontend to Netlify
(`DEPLOYMENT.md` Step 4) — this was tested against localhost only. The
demographics endpoint (`/api/parcels/{id}/demographics`) is also still
unwired in the dashboard — no UI element calls it yet.

## Backend + frontend deployed live, 2026-07-03/04

Backend deployed to Render: `https://enclave-scanner-backend.onrender.com`
(free tier). Confirmed live post-deploy: `/health`, and real scans against
Pasco and Osceola (the TLS-cert-bundling county) both returned the same
results seen locally, including outbound calls to live ArcGIS servers and
Osceola's bundled Entrust intermediate cert working from Render's network.

Frontend deployed to Netlify: `https://enclave-scanner-backend.netlify.app`.
Netlify's build had to be scoped with **Base directory = `web`** — its
build image auto-detected the repo-root `runtime.txt`/`requirements.txt`
(which exist for the Python backend, deployed separately via Render) and
tried to provision a Python toolchain for what should be a zero-build
static-file deploy, failing because `mise`/`pyenv` had no precompiled
build for the exact pinned patch version. Scoping the base directory to
`web/` stops Netlify from seeing those root-level Python files at all —
**do not** "fix" this by changing `runtime.txt`'s version; that file is
load-bearing for Render's backend and unrelated to the frontend failure.

`web/index.html`'s default API base URL now points at the live Render
URL (was `localhost:8000`). Render's `FRONTEND_ORIGIN` is locked to the
exact Netlify origin (no more `*`) — confirmed via direct `curl` with an
`Origin` header that the real Netlify origin gets
`access-control-allow-origin` echoed back and an arbitrary other origin
does not.

Both GitHub accounts involved (`tylerdeclercq0-svg`, repo owner;
`tjd135-rgb`, pushes from this machine) belong to the same person —
`tjd135-rgb` was added as a repo collaborator to fix a 403 on push.

## Demographics endpoint (`ring_demographics.py`) implemented, 2026-07-04

The batched ACS fetch flagged as `NotImplementedError` above is now
implemented, and two more real, previously-undiscovered bugs were found
and fixed live while building/testing it (no `CENSUS_API_KEY` available
yet, but every other piece was verified against the real Census/TIGERweb
servers):

1. **`fetch_acs_values_for_block_groups`**: implemented the
   state/county/tract grouping and per-tract batched call the docstring
   had already sketched out. Confirmed live against the real ACS API
   (with a deliberately fake key) that the request shape itself is
   correct — the server got far enough to reject only the bad key, not
   the query structure. Also discovered live that an invalid/expired key
   returns HTTP 200 with an HTML "Invalid Key" page, not a 4xx or JSON —
   added explicit handling so this fails with a clear message instead of
   a raw `JSONDecodeError`.
2. **`TIGERWEB_BLOCKGROUP_URL` pointed at the wrong service entirely**
   (real bug, not just an unconfirmed layer index as the old comment
   said): `TIGERweb/State_County/MapServer` only has States/Counties
   layers at any index — no block groups exist there at all, so every
   call silently returned zero features. Fixed to
   `TIGERweb/tigerWMS_ACS2023/MapServer/10` ("Census Block Groups"),
   confirmed live (321 real block groups returned within 15 miles of a
   real Pasco parcel centroid, all real 12-digit GEOIDs).
3. **Missing `outSR` on the TIGERweb query** — same class of bug as the
   FLUM neighbor query fixed earlier in this file: without it, geometry
   came back in the service's default Web Mercator (meters) while
   `compute_ring_demographics`'s ring-inclusion check compares distances
   in degrees. Fixed by adding `outSR=4326`.
4. **`shape(geom)` crashed on every real block-group geometry** — Shapely's
   generic `shape()` expects GeoJSON (`{"type": "Polygon", ...}`), but
   TIGERweb (like every other Esri REST source in this project) returns
   Esri JSON (`{"rings": [...]}`) with no `"type"` key at all, so this
   raised `AttributeError: 'NoneType' object has no attribute 'lower'`
   every time. This was already flagged as unfinished in the function's
   own inline comment ("adapt esri_json_to_shapely... if reusing that
   converter") but never actually fixed. Fixed by reusing
   `encirclement.esri_json_to_shapely()` (the same multipart-ring-aware
   converter used for FLUM polygons) instead of `shapely.geometry.shape`.

Also implemented population margin-of-error aggregation (root-sum-of-
squares across included block groups) since it's well-defined and the
`RingDemographics.population_moe` field already existed for it — income
and age medians are deliberately left as `None`, since those need a
population-weighted approach, not a simple sum/RSS, and that's a bigger
follow-up, not a quick fix.

Added an `except Exception` catch-all to `/api/parcels/{id}/demographics`
in `main.py` (previously only caught `NotImplementedError`/`ImportError`),
matching the `/scan` endpoint's existing pattern, since a bad API key
(`RuntimeError`) or a `requests` network error would otherwise surface as
a bare unhandled 500 with no detail.

**Not yet done**: no `CENSUS_API_KEY` exists yet, so the ACS fetch itself
has never been exercised against real Census data end-to-end — only
verified up to "the real server accepts this request shape and rejects
only the key." Get a real key and re-verify once available. The
dashboard also still has no UI element calling this endpoint at all.

## Exact next step

1. Get a real `CENSUS_API_KEY` (https://api.census.gov/data/key_signup.html),
   set it on Render, and re-verify `/api/parcels/{id}/demographics`
   end-to-end against real Census data (only the failure path has been
   confirmed so far).
2. Wire the demographics endpoint into the dashboard (a per-row "pull
   area demographics" action) — no UI element calls it yet.
3. Revisit the unincorporated-status hard filter given the Osceola
   parcel-layer jurisdiction data gap found earlier in this file —
   likely needs a spatial join against the FLUM layer's `Jurisdiction`
   field instead of the parcel layer's own (NULL) jurisdiction fields.
4. Consider re-running the same live `describe_layer` + distinct-values
   spot-check that caught Pasco's wrong `flu_field` against the other
   three counties' FLUM layers, now that there's a concrete example of
   how an "UNCONFIRMED CARRYOVER guess" note in this file turned out to
   be silently wrong in production-relevant code.
5. `max_candidates=25` default in `run_county_scan` has not been timed
   against a real full run yet — worth a rough wall-clock check before
   wiring this into a synchronous web request (see the function's own
   docstring re: free-tier hosting timeouts).

## Known gaps (already flagged, still true, not addressed this pass)

5-year continuous ag-use history, Wekiva/Everglades exclusion
boundaries, public-services availability, single-owner-as-of-1/1/2025
(vs. current owner of record), and the unincorporated-status hard
filter (data now exists for Osceola/`Jurisdiction` and is surfaced as a
manual-review flag in `scan_orchestrator.py`, but is NOT yet enforced as
an automatic exclusion for any county).
