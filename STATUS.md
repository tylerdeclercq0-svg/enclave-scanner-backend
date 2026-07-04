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

**Deployed and confirmed live**: committed as `32c88b3`, pushed to
GitHub, and confirmed via the Render dashboard's own deploy log —
"Deploy live for `32c88b3`" at July 3, 2026, 10:12 PM. Note this only
confirms the code is deployed, not that the ACS fetch works against
real data by itself — `/health`'s `code_version` string is a stale
hardcoded marker from an earlier commit and was NOT bumped here, so
don't use it to verify which commit is live; check Render's deploy log
directly instead.

**`CENSUS_API_KEY` obtained, activated, and confirmed working end-to-end
against real data — 2026-07-04.** Signed up at
https://api.census.gov/data/key_signup.html; the signup flow sends two
separate emails — the key itself, and a second "click here to activate
your key" confirmation link that must be clicked before the key works
(the first live test attempt failed with the "Invalid Key" HTML page
specifically because this activation step hadn't been done yet — this
is a real, confirmed gotcha in Census's signup flow, not a Render/env-var
problem). Set as `CENSUS_API_KEY` on Render's Environment tab (triggers
an automatic redeploy on save). Called
`/api/parcels/35-24-16-0000-00100-0011/demographics?lat=28.41&lon=-82.66`
against the live Render deployment and got real ACS data back for all
three rings:
- 5 mi: population 35,266 (±1,925 MOE), 19,630 housing units, 449/sq mi
- 10 mi: population 197,122 (±5,590 MOE), 98,108 housing units, 627/sq mi
- 15 mi: population 381,925 (±7,527 MOE), 181,452 housing units, 540/sq mi

`median_household_income`/`median_age` correctly came back `null` for
all three rings — expected, since that aggregation is deliberately
unimplemented (see note above, needs population-weighting not a sum).

## Dashboard wired to demographics endpoint — 2026-07-04

Added a "Pull area demographics" button per parcel row in `web/index.html`
(new rightmost table column). Click-only, per-row fetch to
`/api/parcels/{parcel_id}/demographics?lat=...&lon=...` using that row's own
`centroid_lat`/`centroid_lon` (already present on every `ScanResultRow`) —
confirmed this is NOT called as part of `runScan()`/bulk county scan, matching
`main.py`'s docstring intent. Rows with a null centroid (e.g. counties/parcels
where `get_centroid_lat_lon` failed) show a disabled "No centroid" button
instead of a dead click. Results render 5/10/15-mile population (with MOE),
housing units, and density inline in the cell; `median_household_income`/
`median_age` explicitly render as "not available" (styled distinctly, not
blank/zero) since that aggregation is a known, deliberate gap, not a bug.
Failed fetches show a "Failed — retry?" state with a retry button rather than
silently failing. Verified visually in a local static preview (mock data
injected via console since CORS locks the real Render backend to the Netlify
origin only, not localhost) — DOM/render logic confirmed correct; the real
network path was already confirmed working via direct `curl` against Render
in the prior session.

## Statutory gaps — research pass 2026-07-04 (data sources confirmed live, NO CODE WRITTEN YET)

Tyler asked for these three gaps to be closed, in order, with a live-data
research pass reported back before writing any implementation. All three
are researched and confirmed live; nothing below is wired into
`exclusions.py`, `scan_orchestrator.py`, or `county_registry.py` yet — this
is groundwork only, picked back up next session.

### 1. Unincorporated-status hard filter — mixed readiness per county

- **Osceola**: FLUM layer's `Jurisdiction` field already confirmed
  populated (`Unincorporated`/`incorporated`+city). The parcel layer's own
  `Jurisdicti`/`JurisDesc` are NULL (known gap, prior session) — so this
  needs a spatial join: candidate parcel geometry -> intersect against
  Osceola's FLUM layer -> read `Jurisdiction` off whichever FLUM polygon
  contains it. Not yet implemented.
- **Nassau**: already effectively satisfied — the FLUM layer
  (`Unincorporated_Nassau_County_Future_Land_Use_`) is pre-filtered to
  unincorporated land at the source (confirmed via its own title/ownership,
  prior session). No join needed, no further action.
- **St. Johns**: no separate jurisdiction field on either layer (confirmed
  via a fresh field-name grep against both FLUM and parcel layers this
  session — genuinely absent, not a naming guess). Incorporated cities
  appear as their own `FUTLUSE1` categories instead (`CITY OF ST.
  AUGUSTINE`, etc., confirmed prior session) — the filter here is
  excluding those specific FLU category strings, not a spatial join. Not
  yet wired into exclusion logic (currently only informs
  `agricultural_flu_values`, not a separate unincorporated check).
- **Pasco**: no jurisdiction field at all on the FLUM layer. **INFERENCE
  DISPROVEN 2026-07-05** — live point-in-polygon query against the FLUM
  layer (`Land_Use/MapServer/0`) at each incorporated city's City Hall
  coordinate: Zephyrhills, Dade City, New Port Richey, and San Antonio all
  correctly returned 0 features (consistent with unincorporated-only), but
  **Port Richey City Hall (28.2612, -82.7168) returned a real feature** —
  `FLU_CODE='RES-9'`, 266.7 acres, `OBJECTID=949` — not a boundary sliver,
  a substantial designated area. A control point over known unincorporated
  land (28.41, -82.66, the same parcel used elsewhere in this doc)
  correctly hit too, confirming the query itself is sound. **Conclusion:
  the "BOCC layer is unincorporated-only by home-rule construction"
  assumption is FALSE, at least for Port Richey** — this layer cannot be
  used as-is as a proxy for the unincorporated hard filter in Pasco, since
  it would silently pass incorporated Port Richey parcels through as if
  unincorporated. Pasco's unincorporated-status check remains an open gap;
  do NOT wire "any FLUM hit = ok" logic for Pasco. Needs either a real
  Pasco city-limits boundary layer (not yet searched for) or manual-review
  fallback, same as St. Johns' pre-existing gap.

### 2. Wekiva Study Area / Everglades Protection Area exclusion — both real layers found, one real trap avoided

- **Everglades Protection Area**: confirmed live —
  `https://services1.arcgis.com/sDAPyc2rGRn7vf9B/arcgis/rest/services/RULE40E_63_EVERGLADES_PROTECTION_AREA/FeatureServer/0`
  (SFWMD-hosted, 6 real features, matches `exclusions.py`'s cited statute
  s. 373.4592(2), F.S. / Ch. 40E-63 F.A.C.). Not geographically relevant to
  any of the four current pilot counties, but now a real usable layer
  instead of the old placeholder URL.
- **Wekiva — real trap found and avoided**: `exclusions.py` cites s.
  **369.316**, "Wekiva **Study** Area" specifically — legally DISTINCT from
  the more commonly-indexed "Wekiva **River Protection** Area" (WRPA, s.
  369.303/369.301(9), a different part of the same statute chapter). First
  search results (an Orange County layer, an SJRWMD layer) were both WRPA,
  not Study Area — wiring either in directly would have been a real
  conflation bug. Kept digging and found **Seminole County's own layer** —
  `https://services3.arcgis.com/n4VF6lyYfB5kizho/arcgis/rest/services/WekivaProtectionAreas/FeatureServer/0`
  — which has explicit separate `WRPA` and `WSA` yes/no fields on the same
  2 real features (confirmed via live query, e.g. one feature is
  `WSA=yes, WRPA=no`, ~19,739 acres). Filtering `WSA='yes'` gets the actual
  statutory boundary needed. Caveat: this layer's extent looks like it only
  covers the Orange/Seminole border area (~0.1x0.24 degrees), while the
  statute's metes-and-bounds description spans Lake/Orange/Seminole — may
  not capture the Lake County portion. Moot for the four current pilot
  counties (none are in Lake/Orange/Seminole); would matter if Orange
  County (already in the registry as unconfirmed) gets activated later.
  Not yet wired into `exclusions.py` — `WEKIVA_STUDY_AREA_LAYER_URL_PLACEHOLDER`
  is still `None` there.

### 3. Single-owner-as-of-1/1/2025 — real sale-date field confirmed on all four counties, encodings differ

| County | Field(s) | Format | Confirmed via live sample |
|---|---|---|---|
| Pasco | `SALE_YEAR`/`SALE_MON`/`SALE_DAY` + `SALE_AMT` | separate ints, full precision | 2018-05-02, $30,000 |
| Nassau | `SALEYR1`/`SALEPRC1` (+ `SALEYR2`/`SALEPRC2` prior-sale pair) | **year only**, no month/day | 2019, 2018 |
| St. Johns | `SALEDATE` | integer, values like 38520/37741 — near-certainly Excel/OLE serial day count (days since 1899-12-30), NOT YYYYMMDD (wrong magnitude for that) | plausible mid-2000s dates once decoded; **encoding inferred from value magnitude, not confirmed by documentation** |
| Osceola | `SaleDate`/`PrevSaleDa` (+ `SalePrice`) | standard Esri epoch-millis date field | 1690848000000 = 2023-08-01; also has a previous-sale pair, best of the four |

Nassau's year-only granularity is not actually a real limitation for this
specific check: since the cutoff is exactly 1/1/2025, `SALEYR1 >= 2025` is
equivalent to "sold on or after 1/1/2025" with no precision loss. None of
these fields let us reconstruct historical ownership before the
most-recent recorded sale — they only support a "has this parcel changed
hands since 1/1/2025" flag, which is what was asked for (a flag, not full
historical reconstruction). Not yet wired into `scan_orchestrator.py` or
surfaced as a flag anywhere.

## Dashboard updated for the new statutory-gap fields — 2026-07-05

`web/index.html` updated to surface what the backend has returned since
the statutory-gap pass above:
- New "Sold Since 2025" column (Yes in dev-orange / No / italic "unknown"
  for `sold_since_2025 === null`) — also added to the CSV export headers.
- The old single merged "N flags" column (exclusion_flags concatenated
  with needs_manual_review) split into two real columns: "Exclusions"
  (red, bold "N EXCLUDED" badge, or green "clear" if empty — this is
  where a hard unincorporated-filter failure or a real Wekiva/Everglades
  hit now shows up distinctly) and "Review Notes" (muted "N notes" badge
  for the softer manual-review items). Both are still sortable via the
  existing `data-key` header-click mechanism (array-length sort already
  worked generically, no new sort code needed).
- Rewrote the caveats footer, which had gone stale — it previously told
  users Wekiva/Everglades exclusions and the 1/1/2025 sale check were
  "not automated," which is no longer true for 3 of 4 pilot counties.
  Now states plainly what's automated as of July 2026 vs. what still
  needs manual verification (Pasco's unincorporated check specifically,
  conservation easements, military buffers, 5-year ag-use history,
  public services availability).

Verified visually via a local static preview (`.claude/launch.json`'s
`web` config, `python -m http.server 5500 --directory web`) with mock
result data injected via `preview_eval` (same approach as the
demographics-button verification earlier — CORS still blocks the real
Render backend from localhost). Confirmed: the Osceola mock row (with a
real `exclusion_flags` entry) renders "1 EXCLUDED" in red while the other
two mock rows render "clear" in green; "Sold Since 2025" renders
Yes/No/unknown correctly; CSV export string includes `sold_since_2025`
in the right column position.

## Exact next step

All three statutory gaps are implemented, live-verified, committed
(`a6e7db8`), pushed, and confirmed live on Render (2026-07-05). The
dashboard update (`88617de`) is pushed and confirmed live on Netlify
(2026-07-05) as well. Next session should:

1. Consider re-running the same live `describe_layer` + distinct-values
   spot-check that caught Pasco's wrong `flu_field` against the other
   three counties' FLUM layers, now that there's a concrete example of
   how an "UNCONFIRMED CARRYOVER guess" note in this file turned out to
   be silently wrong in production-relevant code.
2. `max_candidates=25` default in `run_county_scan` has not been timed
   against a real full run yet — worth a rough wall-clock check before
   wiring this into a synchronous web request (see the function's own
   docstring re: free-tier hosting timeouts). This pass added two more
   live queries per candidate (unincorporated spatial join + Wekiva/
   Everglades checks), so this is more relevant now than before.

## All three statutory gaps implemented and live-verified — 2026-07-05

New module `app/statutory_checks.py` holds the sale-date decoder
(`sold_on_or_after_cutoff`) and the unincorporated spatial-join check
(`check_unincorporated`), dispatched per-county via two new
`CountyEndpoint` fields (`sale_date_encoding` + `sale_year_field`/
`sale_month_field`/`sale_day_field`/`sale_date_field`, and
`unincorporated_check` + `incorporated_flu_values`).

1. **Pasco go/no-go, resolved NO-GO**: a live point-in-polygon query
   against Pasco's BOCC FLUM layer at each incorporated city's City Hall
   coordinate disproved the "unincorporated by home-rule construction"
   inference — Port Richey City Hall (28.2612, -82.7168) intersects a
   real 266.7-acre `FLU_CODE='RES-9'` feature, while Zephyrhills, Dade
   City, New Port Richey, and San Antonio correctly returned zero
   features. Pasco's `unincorporated_check` is left at `"manual_only"` —
   no automated pass/fail wired in, to avoid a false positive.
2. **Unincorporated hard filter wired in for Osceola/Nassau/St. Johns**,
   via `scan_orchestrator.run_county_scan`'s per-parcel loop calling
   `statutory_checks.check_unincorporated()`. A `False` result now
   appends to `exclusion_flags` (hard filter), not just a soft
   `needs_manual_review` note. Live-verified against real parcels: a
   real Osceola ag parcel (PARCELNO `012527000000400000`) came back
   jurisdiction `'R.C.I.D.'` (Reedy Creek Improvement District, Walt
   Disney's special district) — correctly flagged as NOT unincorporated,
   not a bug; a real St. Johns ag parcel (PIN `010832 0010`) correctly
   passed with no incorporated-city FLU overlap.
3. **Wekiva `WSA='yes'` + Everglades exclusion wired into
   `exclusions.py`**, replacing both placeholder constants with the real
   URLs found in the prior research pass. Also fixed a real latent bug
   while wiring this in: `check_exclusions` was about to query these
   layers using `parcel.geometry` with no `spatialReference` set — same
   "ArcGIS Server omits spatialReference on a feature's geometry" bug
   class already fixed once in `scan_orchestrator._buffer_esri_geometry`;
   `inSR` would have silently defaulted to 4326 and misinterpreted
   AREA_SR (3086) meter coordinates as lat/long. Fixed via a new
   `_with_area_sr()` helper. Live-verified against a real Pasco parcel
   (`35-24-16-0000-00100-0011`) — both queries ran cleanly with no
   errors and correctly found no hits (Pasco isn't geographically near
   either zone).
4. **Post-1/1/2025 sale flag wired into `parcel_fetcher.CandidateParcel`
   and `scan_orchestrator.ScanResultRow`** as `sold_since_2025:
   Optional[bool]`. Sale-date decoding logic unit-verified standalone
   against the exact sample values recorded in this file (Pasco
   2018-05-02 → False, 2025-01-01 → True, 2024-12-31 → False; Nassau
   2019 → False, 2025 → True; St. Johns serial 38520 → False; Osceola
   epoch-millis 1690848000000 (2023-08-01) → False). Live-verified end-
   to-end against a real Pasco parcel — `SALE_YEAR`/`SALE_MON`/
   `SALE_DAY` came back `None` on that specific row (field exists and is
   populated on other Pasco parcels per this file's earlier
   confirmation; just null on this one) — correctly surfaces as "could
   not be determined," not a guessed `False`.

No committed unit test suite exists in this repo (searched — none
found), so all of the above was verified via live one-off scripts against
the real ArcGIS endpoints, then deleted; not left behind as test files.

## Next-step follow-ups closed out — 2026-07-06

1. **FLUM field-name spot-check re-run against Nassau, St. Johns, Osceola**:
   live `describe_layer` + distinct-values queries against all three
   confirm zero drift — `flu_field`/`jurisdiction_field`/
   `agricultural_flu_values`/`incorporated_flu_values` in
   `county_registry.py` all still match the real live schema exactly as
   documented above (e.g. Nassau's `FLUM` field still returns
   `'Agriculture'` among 22 real distinct values; St. Johns' `FUTLUSE1`
   still has all three `incorporated_flu_values` entries present among 28
   categories; Osceola's `FLU`/`Jurisdiction` fields and
   `'rural/agricultural'` value confirmed present). No code changes
   needed — this was a verification pass only, run via a deleted one-off
   script (same convention as prior live checks in this file).
2. **`max_candidates=25` timed against a real local run**: Pasco — 25
   candidates in 48.4s (1.94s/candidate); Osceola — 25 candidates in
   54.8s (2.19s/candidate), the more expensive path since it's the one
   county running the unincorporated FLUM spatial join per-candidate on
   top of the encirclement + Wekiva/Everglades checks. Both measured
   locally (`.venv312`, no Render network hop), so a real Render-hosted
   request will run slower than this. ~50s for the current default is
   already close to common free-tier reverse-proxy timeout thresholds
   (e.g. Heroku's router hard-cuts at 30s; Render's own limit is less
   strictly documented but not unlimited) — worth lowering the default
   `max_candidates` (e.g. to ~10–15) or moving to a background-job/poll
   pattern before this is relied on for a real production scan, per the
   function's own docstring caveat. Not changed this pass — a timing
   measurement only, no code changed.

## Session 2026-07-06: rebuild to match "Falcone Group v3" mockup + Ledger & Brass restyle

Tyler asked for a pure visual restyle of `web/index.html` to a new "Ledger &
Brass" design direction (tokens below). While scoping that, he shared
screenshots + two PDFs of a hand-built mockup ("Enclave Scanner v3") showing
a materially different UX he actually wanted: a 4-step wizard, richer
filters, and parcels grouped into Confident/Possible/Unlikely tiers. He
confirmed (via plan-mode questions) this supersedes the restyle-only
request — it's a real feature rebuild, not a CSS swap. Full plan is still
on disk at `C:\Users\tyler\.claude\plans\jolly-orbiting-pudding.md` if the
reasoning behind any decision below needs to be re-derived.

**Before the pivot**, two small pieces were also built and verified this
session, on top of the OLD single-scroll-page dashboard (now superseded by
the rebuild below, but the underlying backend/JS logic they added was
carried forward):
- A parcel detail overlay (click a result row for full detail: facts,
  encirclement bar + pathway descriptions, score breakdown, exclusions/
  review notes, inline demographics pull).
- A "How to use this tool" panel, a "What this does / doesn't do" panel
  (split out of the old single caveats footer), and a downloadable
  Manual Review Checklist export (`.txt`) alongside the existing CSV
  export.

### Decisions locked with Tyler before implementing

- **Counties**: keep all of them in the registry (the mockup's 7 —
  Hillsborough, Orange, Pasco, Sarasota, Manatee, Brevard, Volusia — plus
  Nassau/St. Johns/Osceola, the 4 more rigorously ground-truthed this
  project has since added). Live/confirmed ones selectable; unconfirmed
  ones (Orange, Sarasota, Manatee — field names/URLs never
  describe_layer-verified) shown shaded "coming soon."
- **Water/sewer estimate**: research a real source now, not a stub. Found
  and live-verified — see below.
- **Map view**: deferred as a follow-up. List view only, built fully.
- **Restyle timing**: Ledger & Brass tokens applied directly on the new
  wizard structure, not on the old page first.

### Ledger & Brass design tokens (now live in `web/index.html`)

```
--ink:#1e2a24        --paper:#faf9f6      --paper-raised:#ffffff
--line:#e2ddd0       --line-strong:#d8d3c4
--brass:#8a6d1f      --brass-dim:#eee3c2  --brass-800:#6b5218
--clay:#8b3a2f       --clay-dim:#f3ded9   --ink-faint:#8a8574
```
Display/headers: Source Serif 4 (700). Body/UI: Inter. Data/labels/mono
(eyebrows, table headers, badges, parcel IDs, coordinates): IBM Plex Mono.
2-4px border-radius, 1px hairline borders as the primary separator (not
shadows), no gradients. Score/status pills use brass-dim/brass-800 for
positive states and clay/clay-dim for exclusions/warnings — no red/green
traffic-light colors anywhere.

### New real data source found and live-verified: FDOH's FLWMI (water/sewer)

`https://gis.floridahealth.gov/server/rest/services/FLWMI/FLWMI_Wastewater/MapServer/0`
— a real statewide parcel-level layer with `WW` (wastewater) / `DW`
(drinking water) coded values that already bake in a confidence tier
(`Known*`/`Likely*`/`SWL*` = Somewhat Likely/`UNDT`/`UNK`/`NA`), exactly
matching what the mockup claimed existed. New module `app/flwmi_client.py`
wraps it. Two real things confirmed live, not assumed:
- This host silently returns the HTML "ArcGIS REST Services Directory"
  page (still HTTP 200) if `f=json` is sent in a POST body instead of the
  URL query string — the opposite of every other ArcGIS host in this
  project (which all accept POST fine, per `arcgis_client.py`'s existing
  design). `flwmi_client.py` deliberately bypasses `arcgis_client.query_layer`
  and does a plain GET instead, since this lookup never needs geometry
  anyway.
- `PARCELNO` join key format matches each county's own parcel-ID field
  exactly for Pasco and Osceola (confirmed with real live samples); for
  St. Johns it does NOT (`PIN` is space-separated, FLWMI's is not) — a new
  `CountyEndpoint.flwmi_parcel_id_transform="strip_spaces"` handles it,
  confirmed live. Nassau's format looks consistent but hasn't been
  cross-checked against one specific real Nassau `PARCELID` sample yet.

### Real bug found and fixed: `exclusion_flags` was never actually empty

`exclusions.check_exclusions()` used to unconditionally append 3
boilerplate "not automated, verify manually" reminders (ACSC, conservation
easements, military buffers) into the SAME list as genuine hard hits
(Wekiva/Everglades/unincorporated-check failures). That meant every real
parcel's `exclusion_flags` was non-empty even when nothing actually
applied — silently defeating the dashboard's "clear" vs. "N EXCLUDED"
distinction added earlier this session, and making a "no manual review
needed" confidence tier structurally impossible. Fixed: the 3 boilerplate
reminders moved to a new `exclusions.standing_manual_notes()`, merged into
`needs_manual_review` unconditionally in `scan_orchestrator.py` instead.
Live-verified end-to-end against a real Pasco scan — `exclusion_flags`
now genuinely comes back `[]` for parcels with no real hit.

### New backend fields/logic, all live-verified against a real Pasco scan

- `single_owner_signal` (`CandidateParcel`/`ScanResultRow`) — `True`/`False`
  from whether `owner_name_2` is populated on the parcel's own record,
  `None` when a county has no co-owner field at all (Nassau, St. Johns) —
  deliberately not assumed single-owner just because the data's missing.
  `require_single_owner` (previously a dead, unused field on `main.py`'s
  now-deleted `ScanRequest`) is now a real query param that filters
  server-side in `parcel_fetcher.fetch_candidate_parcels`.
- `flu_taxonomy.py` (new): `classify_density()` — a keyword classifier
  (rural/suburban/urban/unknown) applied to the dominant neighboring FLU
  value already available per-segment in `EncirclementResult.segments`
  (no new query); `determine_own_flu()` — the candidate's own current FLU
  designation, found via area-overlap against the same `neighbor_features`
  already fetched for encirclement (also no new query). Verified live:
  works well against descriptive FLU strings (Osceola's
  `'rural/agricultural'`, St. Johns' `'AGRICULTURE'`); legitimately comes
  back `"unknown"` for Pasco's abbreviated codes (`RES-6`, `PD`) since
  those aren't descriptive keywords — an honest gap, not a crash or a
  guess, flagged as a real limitation of this keyword approach.
- `scoring.classify_confidence()` — Confident/Possible/Unlikely, unit- and
  live-verified against all three outcomes: Unlikely = no pathway matched
  at all; Confident = a pathway matched AND no real exclusion hit AND no
  co-owner on record AND water/sewer confidence is Known/Likely; Possible
  = pathway matched but something above isn't confirmed.
- `county_registry.py`: added `population` (approximate 2024 Census
  estimates, public data, not live-queried) and `confirmed_live` (explicit
  per-county flag, replacing `main.py`'s old ad hoc `flu_field` heuristic)
  for all 10 counties.
- `/api/counties/{id}/scan` gained real query params: `require_single_owner`,
  `min_encirclement_pct`, `surrounding_density`. `flum_character` filtering
  exists in `scan_orchestrator.run_county_scan()` but isn't wired to a
  populated dropdown in the UI yet (no per-county FLU category list built
  out — left as "Any" only, honestly, rather than a fake-looking dropdown).

Live end-to-end confirmation: a real Pasco scan (5 candidates) came back
with correct, differentiated `confidence_tier`, `single_owner_signal`,
real FDOH `water_source`/`wastewater_method`/`water_sewer_confidence`, and
`flum_character` values — genuinely wired, not synthetic.

### Frontend: `web/index.html` fully rebuilt as a 4-step wizard

Masthead (brass eyebrow "FALCONE GROUP" + serif "Enclave Scanner" title) →
step bar (Select county → Run scan → Review candidates → Verify and
export, Back/Continue nav, state persists client-side across steps, no
reloads) → Step 1 county cards (population, shaded "coming soon" for
unconfirmed counties) → Step 2 filter panel (Size & Ownership: acreage,
DOR-range display-only dropdown, ownership dropdown wired to
`require_single_owner`; Character & Density: FLUM character (display-only
for now), surrounding density, min encirclement %, all wired) → Step 3
results grouped into Confident/Possible/Unlikely (Unlikely collapsed by
default, "Show N unlikely properties" toggle), row click reopens the
parcel detail overlay (now extended with water/sewer, ownership signal,
FLUM character/density, confidence-tier badge) → Step 4 keeps the CSV +
Manual Review Checklist exports and adds a new "Your next steps, in
order" panel — a real ordered narrative generated from each
selected/scanned parcel's actual flags (title work, Property Appraiser
call, public-services confirmation referencing the real water/sewer
estimate, Planning Dept call re: exclusion zones/Option E, re-review of
"possible"-tier parcels). "How to use this tool" and "What this does /
doesn't do & methodology" are now overlay panels with content adapted
from Tyler's two reference PDFs, corrected to reflect this project's
actual current automation status rather than the mockup's claims.

Verified via the preview tools (`.claude/launch.json`'s `web` config,
mock `results`/`counties` injected via `preview_eval` since CORS still
blocks the real Render backend from localhost, same approach as prior
sessions): county shading, step navigation, tiered grouping + unlikely
toggle, the extended detail overlay, the Step 4 next-steps panel, and the
"what this does" info overlay all confirmed rendering correctly with the
new tokens, no console errors.

**Deliberately left out, not an oversight**: the mockup's "CERTIFICATION
WINDOW CLOSES JAN 1, 2028" badge and its "SB 686 / CH. 2026-34" chapter-law
citation (vs. this codebase's existing "SB 686 / HB 691, F.S.
163.3164(4)" citation used everywhere else). Neither could be verified
against anything already in this project — asked Tyler whether he has the
real enrolled-bill chapter number and certification deadline; waiting on
that answer before adding either, rather than presenting an unverified
legal date/citation in a tool meant to be relied on for legal screening.

## Known gaps (still true, not addressed this pass)

5-year continuous ag-use history and transportation/schools/recreation
public-services availability remain fully unaddressed (no data source
found). Water/sewer specifically is now Estimated, not a gap — see FLWMI
above. Pasco's unincorporated-status check remains manual-review-only
(see NO-GO above). ACSC layer is still an unresolved Hub-page placeholder,
not a real FeatureServer endpoint. Conservation easements and military
installation buffers still have no automatable data source at all. Map
view is deferred (List view only, built fully). `flum_character` filtering
has no populated per-county dropdown yet (backend supports it; UI doesn't
expose real options). Nassau's FLWMI `PARCELNO` join format is assumed,
not cross-checked against one specific real sample yet.

## Dashboard docs rewrite — 2026-07-06

`web/index.html`'s two static info overlays (`HOW_TO_USE_HTML`,
`buildWhatThisDoesHtml()`) were rewritten at Tyler's request, using Tyler's
"Enclave Scanner v3" reference PDF as a structural model but corrected
against the real code (`encirclement.py`, `scoring.py`, `exclusions.py`,
`statutory_checks.py`) rather than the mockup's claims:
- "How to use" — ~600 words, a ~5-minute action-oriented walkthrough, no
  legal content.
- "What this does/doesn't do & methodology" — ~2,500 words, a ~15-minute
  read covering the bill in plain terms, the six statutory requirements, all
  five encirclement Options A-E with each one's REAL status, the exclusion
  zones, the full automation-status table, per-county unincorporated-check
  status (including the Pasco NO-GO story), scoring methodology, confidence
  tiers, and public-services/demographics caveats.
- Real finding surfaced by this rewrite, not previously stated this
  plainly: Options B, C, and D are not merely "estimated" as the old copy
  said — they are **structurally unreachable** today.
  `scan_orchestrator.py` never passes `designated_pct_existing_development`
  (stays `None`, so Option B can never trigger) and hardcodes
  `adjacent_to_interstate=False` / `adjacent_to_usb=False` (so Options C and
  D can never trigger either). Only Option A (≥75% perimeter existing
  development) can currently ever match. This means every "Confident"/
  "Possible" result in this build today qualified through Option A alone —
  worth remembering when prioritizing the punch list below, since wiring in
  real interstate/USB data is the single highest-leverage fix available.
- Deliberately used only the codebase's already-verified citation (SB 686 /
  HB 691, F.S. 163.3164(4), eff. 7/1/2026) — Tyler confirmed NOT to add the
  mockup's unverified "Ch. 2026-34" / "certification window closes Jan 1,
  2028" language.
- Verified via the preview tools: both overlays render with no console
  errors, correct Ledger & Brass styling on headings/tables/bullets, word
  counts land at target (~616 / ~2,477 words).

## Punch list — next priorities, 2026-07-06

**High priority (unlocks real functionality):**
1. Deploy this session's committed work (`1be635c` + the docs rewrite above)
   to Render/Netlify — nothing has shipped since `88617de`. **Still open —
   pending Tyler's go-ahead.**
2. Wire a real FDOT interstate-adjacency layer + per-county urban-service-
   boundary (USB) layers. This is the single highest-leverage fix — it's
   what makes Options B, C, and D structurally dead today (see above).
   **DONE for interstate adjacency (all 4 counties) + USB (Pasco only, via
   approximation) — see the two follow-up sections below. Nassau/St. Johns/
   Osceola still have no USB data source; Option B remains unreachable
   everywhere (different missing data, not addressed this pass).**
3. Find a rural-study-area dataset per county to unlock Option E. **Still
   open.**
4. Resolve the ACSC Hub-page URL
   (`mapdirect-fdep.opendata.arcgis.com/maps/areas-of-critical-state-concern`)
   into a real, queryable FeatureServer endpoint. **DONE — see below.**

**Medium priority:**
5. Pasco's unincorporated-status check — needs a real city-limits boundary
   layer (currently manual-only; the FLUM-layer inference was disproven via
   the live Port Richey test). **DONE — see below.**
6. Auto-detect the statute's 4,480-acre dense/rural exception instead of
   requiring a user to manually raise the acreage filter.
7. Cross-check Nassau's FLWMI `PARCELNO` join format against one real Nassau
   sample (currently assumed consistent, never confirmed).

**Lower priority / longer-term:**
8. Conservation easements and military installation buffers — no statewide
   GIS source exists; would likely need a fundamentally different approach
   (per-county Clerk/Recorder scraping, DOD compatibility-zone layers, etc.).
9. Population-weighted median household income / median age aggregation for
   area demographics (currently deliberately `null`).
10. Populate the `flum_character` filter dropdown in the UI with real
    per-county FLU category options (backend already supports the filter).
11. Map view (deferred by Tyler's own prior scope call — List view only).

## Punch-list items #2 and #4 worked — 2026-07-06

**#4 (ACSC endpoint) — RESOLVED.** The Hub page was never a real endpoint;
found the underlying FeatureServer via ArcGIS Online's item-search API
(`www.arcgis.com/sharing/rest/search`), owner `FDEPMapDirect`:
`ca.dep.state.fl.us/arcgis/rest/services/Map_Direct/Program_Support/MapServer/5`.
Confirmed live: 5 real features (Apalachicola, Green Swamp, Florida Keys, Key
West, Big Cypress — matching `exclusions.py`'s own long-standing citation),
none listing any of the seven pilot counties in `CNTYS`. Wired into
`exclusions.py` as a real automated hard-exclusion check (previously a
permanent manual-review note) — live-verified two ways: a real Pasco parcel
still correctly comes back clear, and a synthetic point built from a real
point inside the Green Swamp ACSC polygon correctly returns a hit. ACSC
dropped out of `standing_manual_notes()`; only conservation easements and
military buffers remain there now.

**#2 (interstate/USB adjacency) — HALF RESOLVED.** New module
`app/roads_client.py` wraps FDOT's own `RCI_Layers/FeatureServer/7`
("Interstates") — a real statewide polyline layer, confirmed live with a
`COUNTY` field whose plain-English spelling matches this project's own
`county_registry.py` `name` field for all four pilot counties (Pasco has
I-75/I-275, Nassau has I-10, St. Johns has I-95, Osceola has I-4).
`adjacent_to_interstate` in `scan_orchestrator.py` and `scoring.py` is no
longer hardcoded `False` — it's a real buffered spatial intersection
against this layer, live-verified three ways: a real Pasco candidate parcel
correctly returns `False` (it's nowhere near I-75), a synthetic polygon
built to straddle a real point on I-75 (fetched live, in the same AREA_SR
Florida Albers projection used everywhere else in this project) correctly
returns `True`, and the same polygon offset 50km away correctly returns
`False` again.

Interstate adjacency alone does NOT unlock Options C/D, since both also
require `adjacent_to_usb` — searched live via ArcGIS Online's search API for
a USB/urban-service-boundary layer for Pasco, Nassau, St. Johns, and Osceola
specifically; found nothing named as such for any of the four (only
Hillsborough is confirmed, per `county_registry.py`'s pre-existing note, to
have a real dedicated USB layer). See the follow-up below for what was found
for Pasco specifically once this was dug into further.

## Punch-list #2 (continued) and #5 — Pasco USB approximation + real city-limits layer, 2026-07-06

Kept digging on the USB gap and found two more real, live, previously-
unknown Pasco_BOCC/`djohnson_pascocounty`-owned ArcGIS Online items via the
same item-search API:

**#5 (Pasco unincorporated-status check) — RESOLVED.** Found a real,
dedicated `City_Limits` FeatureServer (owner `Pasco_BOCC` directly, created
2025-01-23):
`services6.arcgis.com/Mo4MddfRHpFwT7UF/arcgis/rest/services/City_Limits/FeatureServer/0`,
a `CITYNAME` field with real per-city polygons (New Port Richey, Port Richey,
San Antonio, St Leo, Dade City, etc). This is a different, independent
dataset from the FLUM layer that disproved the earlier home-rule inference —
confirmed live two ways: a point built from a real Port Richey polygon
fragment's own centroid correctly returns `CITYNAME='Port Richey'`, and the
prior known-unincorporated control point (28.41, -82.66) correctly returns
no hit. New `CountyEndpoint.unincorporated_check` mode
`"city_limits_layer_join"` added in `statutory_checks.py`, replacing Pasco's
`"manual_only"`. Live-verified end-to-end: a real Pasco candidate parcel now
correctly returns "outside every incorporated city's limits" instead of a
manual-review note.

**#2 (USB, Pasco only) — approximated, not fully resolved.** Pasco's own
comprehensive plan (Map 2-22, "Urban Service Area / Rural Area / Expansion
Area", confirmed via Municode) draws a binary-ish boundary, and a real
`RuralAreas_Current` FeatureServer exists
(`services6.arcgis.com/Mo4MddfRHpFwT7UF/arcgis/rest/services/RuralAreas_Current/FeatureServer/0`,
5 real polygons, `gensis` field references real ordinances like "ORD 25-15").
No layer named "Urban Service Area" or "Expansion Area" itself was found in
the same org, despite searching — so this is treated as an approximation:
`roads_client.check_adjacent_to_usb()` treats a parcel as USB-adjacent if its
buffered boundary is NOT entirely `esriSpatialRelWithin` a single Rural Area
polygon. Confirmed live: a point built inside a real Rural Area polygon
correctly returns "within" (not USB-adjacent), a point in downtown New Port
Richey (clearly urban) correctly returns no "within" hit (USB-adjacent).
Caveat, worth remembering: the Rural Area layer's total area is only ~32% of
Pasco's county area, and the map's title implies a third "Expansion Area"
category this simple binary check can't distinguish from true USB — so this
can plausibly over-count USB-adjacency for land that's actually in an
Expansion Area, not the Urban Service Area itself. A `needs_manual_review`
note is now appended whenever this approximation is used, specifically
telling the user to confirm any Option C/D match with Pasco's Planning
Department.

Wired into `scan_orchestrator.py`/`scoring.py` (`adjacent_to_usb` no longer
hardcoded `False` for Pasco specifically — `county.rural_area_layer_url` is
`None` for Nassau/St. Johns/Osceola, so they're unaffected and still
correctly return `False`). Live end-to-end confirmation: Pasco's own
reference candidate parcel (`35-24-16-0000-00100-0011`) now matches
**pathways [1, 4]** (previously just [1]) — Option D (≤700 ac, ≥50%
encircled, USB-adjacent) is now genuinely reachable for Pasco, score 57→75.
Tier for this specific parcel is "possible", not "confident" — but that's
because its `water_sewer_confidence` comes back "Unknown" (no FDOH record
found for it), unrelated to the USB approximation; `classify_confidence`
doesn't weight which specific pathway matched. Nassau/St. Johns/Osceola
remain unaffected and unchanged, confirmed via a live 2-candidate Nassau
re-run.

Options C/D remain unreachable for Nassau, St. Johns, and Osceola — no USB
data source was found for any of them.

## Punch-list #3 (Option E / rural study area) — investigated, deliberately NOT wired in

Tried to find a real dataset for encirclement pathway 5. Ran into a real
provenance problem worth flagging rather than papering over: "rural study
area" (used throughout this codebase — `encirclement.py`, `web/index.html`,
this file) is this project's own paraphrase, not a verbatim statutory quote
— no file in this repo actually cites the bill's exact defined term for this
option, unlike the other pathways' percentages (75%/75%/50%), which
`encirclement.py`'s own comment says were checked against "the enrolled bill
text." Fetching the Florida Legislature's own current published text of s.
163.3164(4) turned up only two "surrounded by development" options (matching
Options A/B) — no options 3/4/5 at all — meaning either the fetch was
incomplete or the currently-published statute page doesn't yet reflect the
SB 686 amendment (not effective until 7/1/2026). Either way, this means
Option E's exact legal trigger could not be independently re-confirmed this
pass.

A plausible-looking candidate dataset exists — Florida's statewide "Rural
Land Stewardship Area" (RLSA) boundary layer (authorized under a DIFFERENT
statute, s. 163.3248, a stewardship-credit-trading mechanism, findable via
Florida's Geographic Data Library) — but wiring this in without confirming
it's legally the same concept as whatever "rural study area" means in SB 686
would repeat exactly the mistake this project already caught and avoided
once before (Wekiva Study Area vs. the legally-distinct Wekiva River
Protection Area, see the "real trap avoided" section above). Deliberately
did NOT wire in RLSA data. Option E stays unimplemented, `encirclement.py`
still always returns `False` for it. If picked up again: get the actual
enrolled bill/session-law text directly (from Tyler or a verified legislative
source) for s. 163.3164(4)(c)5's exact wording before searching for a
matching dataset, rather than searching from this project's own paraphrase.

Not yet done: #1 (deploy to Render/Netlify — pending Tyler's go-ahead since
it's a live-system push).

## Deployed and confirmed live — 2026-07-06

Tyler gave the go-ahead to deploy once the feasible high-priority punch-list
items above were done. Committed as `ffd24f7` (on top of `1be635c`, which was
still unpushed from the prior session) and pushed to GitHub.

**Confirmed live via real requests, not just the deploy log:**
- **Render backend** (`enclave-scanner-backend.onrender.com`): ran a real
  live scan against Pasco (`/api/counties/pasco/scan?max_candidates=2`) — the
  reference parcel (`35-24-16-0000-00100-0011`) now returns
  `likely_pathways: [1, 4]` (Option D newly reachable, not just Option A as
  before this session), the new "Urban-service-area adjacency... approximated
  from this county's own Rural Area boundary..." manual-review note is
  present verbatim, and `access_score` (70) reflects the real USB signal.
  This confirms the new `roads_client.py` module, the ACSC exclusion check,
  and the Pasco city-limits/USB-approximation logic are all genuinely live
  and running against real ArcGIS data from Render's network, not just
  present in the repo.
- **Netlify frontend** (`enclave-scanner-backend.netlify.app`): fetched the
  live page HTML directly and confirmed it contains this session's specific
  new copy — the "A 15-minute read" / "A 5-minute walkthrough" overlay
  subtitles, and the newer "Reachable for Pasco only" / "city-limits layer
  instead of" language added partway through this session (i.e. not a stale
  cached build from before the Pasco USB/city-limits follow-up work).

As before, `/health`'s `code_version` field is a stale hardcoded marker from
an earlier commit — do not use it to verify which commit is live; this
confirmation used real functional behavior instead (matches the same
verification philosophy used for the `32c88b3` deploy earlier in this file).

## Open items after this session

Everything from the punch list is resolved or deliberately deferred with a
documented reason, except:

- **Option E (rural study area) is the one open item**, and it's blocked on
  a real input, not on more research time: the actual enrolled bill/
  session-law text for s. 163.3164(4)(c)5's exact wording. Every term this
  project has used for it so far ("rural study area") is this project's own
  paraphrase, never verified against the bill itself — the public Florida
  Legislature statute page didn't help (see above). Do not wire in any
  dataset for this (RLSA or otherwise) until the real statutory language is
  in hand; get it from Tyler directly before spending more research time
  guessing at it.

## Enrolled bill text obtained + four gap-closes — 2026-07-06 (late session)

Tyler provided the real enrolled bill URL
(flsenate.gov/Session/Bill/2026/686/BillText/er/HTML, ENROLLED CS for CS
for CS for SB 686 = Ch. 2026-34) and quoted the actual statutory language
for the three items this project had been guessing at (rural study area,
perimeter ROW substitution, acreage cap exception).

**Enrolled bill (c) structure — verified word-for-word, corrects a long-
standing project mislabeling**: (c)'s preamble is *"Are surrounded on at
least 75 percent of their perimeter by:"* followed by (c)1 with three OR-
separated sub-alternatives (a/b/c), then (c)2, then (c)3. This project
labels them Options 1-5 for continuity, but the statute-to-Option mapping
is: Option 1 = (c)1.a; Option 2 = (c)1.b; Option 3 = (c)1.c; Option 4 =
(c)2; Option 5 = (c)3. The `s. 163.3164(4)(c)5` citation this file has
used above and in prior comments/docs is WRONG — Option 5 is `(c)3`.
Left prior sections above unedited for historical accuracy; new work in
this session uses the corrected citations.

**Two additional under-specifications caught while mapping code to bill**
(both worth remembering but NOT fixed this session, punch-list items):
- Option 3 currently checks `adjacent_to_interstate and adjacent_to_usb
  and pct_qualifying >= 75` — but `pct_qualifying` only counts FLUM-
  neighbor segments, not the interstate's own perimeter segment. The
  statute says the 75% is the interstate + designated-USB parcels
  combined. Under-scoring parcels where interstate frontage should count.
- Option 4 currently checks `adjacent_to_usb` as a boolean touch test.
  The statute requires >=50% of perimeter INSIDE a USB, not just touch.
  Over-inclusive.

**One additional real gap flagged from reading (4)(f)**: the enrolled
bill restricts eligibility to "a county with a population of 1.75 million
or less." Not enforced anywhere currently. All ten counties in the
registry are under this threshold, so no observed effect today, but a
future Miami-Dade/Broward addition would silently pass. Small defensive
fix, punch-list.

### #1 (statutory clarification) — DONE, findings above.

### #2 (ROW/water/canal perimeter substitution) — DONE, live-verified.

`encirclement.py` now implements the (4) preamble's substitution rule
via new `ROW_SUBSTITUTION_FEET = 150.0` module constant + a new
`row_substitution_feet` parameter on `compute_encirclement`. For each
FLUM neighbor, the function now measures `max(direct_intersection,
buffered_neighbor_intersection)`, letting a residential FLUM polygon
"reach across" a road/canal gap to the candidate. Existing >=100%
qualifying cap protects against double-counting when two neighbors both
reach into the same ROW gap.

Live-verified against 15 real Pasco candidates via `.venv312`. Sample:
```
parcel_id                        ac  before   after   delta
35-25-16-0030-05700-0000       36.9   47.7%   83.4%  +35.7pp  <- crosses 75% Option 1 threshold
12-24-16-0000-00100-0070       72.2    3.2%   48.0%  +44.8pp
36-25-16-0010-05400-0000       69.0   16.8%   60.4%  +43.6pp
01-26-16-0010-00000-0180       59.0   25.6%   63.2%  +37.6pp
36-25-16-0000-00200-0000       20.0   91.7%  100.0%   +8.3pp
35-24-16-0000-00100-0011       32.9  100.0%  100.0%   +0.0pp  <- reference parcel unchanged, expected
```
Money row: parcel `35-25-16-0030-05700-0000` crosses 75% threshold —
was silently no-pathway before this fix, now matches Option 1. Sanity:
4 parcels already at 100% stay at 100%.

### #3 (acreage cap exception, s. 163.3164(4)(e)) — DONE, honest partial automation.

Previously the fetch cap was a flat 1,280 acres in `parcel_fetcher.py`,
`scan_orchestrator.py`, `main.py`, and `web/index.html` — so parcels
1,280-4,480 acres were dropped BEFORE encirclement was measured; the
exception could never fire. Raised the default cap to 4,480 (the
statute's absolute ceiling) in all four places. Parcels >1,280 acres now
get either a specific manual-review note (if pct_qualifying >= 75,
explaining the buildout-density condition that isn't automatable) or a
hard exclusion flag (if pct_qualifying < 75, statutorily ineligible).
The buildout-density check itself (>=1,000 residents/sq mi at buildout)
remains unautomated by design — would need per-county FLU-category
residents-per-sq-mi coefficients this project doesn't have.

### #4 (Option 5 / rural study area, s. 163.3164(4)(c)3) — DONE, four-county comp-plan review, wired False everywhere with per-county reasoning.

Direct comprehensive-plan review (not GIS layer search), findings
recorded inline in `scan_orchestrator.py`:
- **Pasco**: Northeast Pasco Rural Area is preservation-oriented; plan
  requires a concurrent rural-boundary amendment for higher-density
  applications. Opposite of "intended to be developed with residential
  uses." -> False.
- **Nassau**: 2030 plan discourages rural development; 2050 vision
  preserves rural character. -> False.
- **St. Johns**: 2050 plan's Rural/Silviculture + Agricultural-Intensive
  designations are preservation-oriented. -> False.
- **Osceola**: has an 8,517-acre "study area" for Mixed-Use Districts 5
  & 6 drafted for 14,010 residential units, BUT described in the
  county's own materials as inside "the county's urban service area."
  Ambiguous under (c)3. Conservatively False + Osceola-specific per-
  parcel manual-review note flagging this ambiguity for direct planning-
  department confirmation.

`encirclement.determine_pathways()` now accepts `inside_rural_study_area:
bool` (previously Option 5 had NO code at all — the function returned
after Option 4). All four counties evaluate False today. When this
changes (Osceola confirmation, or new county added), value gets set via
a CountyEndpoint field + boundary check — NOT by falling back to the
statewide RLSA layer, which is a legally distinct s. 163.3248 mechanism
(the trap this project already avoided once with WSA vs. WRPA).

Live end-to-end scan verified via `.venv312` (real Pasco, 5 candidates)
— no exceptions, reference parcel still returns [1, 4], other parcels
show real differentiated qualifying percentages consistent with the ROW-
fix demo above, new statutorily-correct citations in "No pathway matched"
review note.

## Punch list — carryovers after this session

**High-value under-specifications caught but NOT fixed this session:**
1. Option 3 pct_qualifying test doesn't credit interstate frontage —
   only FLUM neighbors count toward the 75%. Real parcels with mostly-
   interstate perimeter are under-scored. Would need to add interstate
   segment length to `qualifying_perimeter` in `compute_encirclement`
   when the parcel is interstate-adjacent.
2. Option 4 USB test is a boolean touch instead of the >=50% USB
   perimeter the statute requires. Currently over-inclusive. Needs
   Pasco's Rural Area layer used as a real perimeter-percent test
   (analogous to how FLUM neighbors are already measured).
3. County population cap (>=1.75M excludes a county entirely,
   s. 163.3164(4)(f)) — not enforced. Add to `CountyEndpoint.population`
   check in `main.py` `scan_county()`.

**Deploy pending Tyler's go-ahead** — this session's work is committed
locally but nothing has been pushed since `1f28ed8` (which is itself
still unpushed on `main`).

## Options 3/4 under-specifications fixed + population cap enforced — 2026-07-06 (very late session)

All three items from the just-added punch list closed. Not yet committed
— Tyler asked to review the deltas before commit/push this time.

### #1 (Option 3 interstate frontage credit) — DONE, wired but with a real "no visible delta" caveat.

`roads_client.py` gained `measure_interstate_frontage_meters()` — buffers
the FDOT interstate polylines by `INTERSTATE_ROW_HALFWIDTH_FEET = 150.0`
(half of a typical 300-ft mainline ROW per FDOT standards) into an
approximate ROW polygon, measures `candidate.boundary.intersection(ROW).length`.
Returns meters, capped elsewhere against total perimeter.
`encirclement.determine_pathways()` now takes `interstate_frontage_pct`,
adds it to `pct_qualifying` for Option 3 (capped at 100), so a parcel
with e.g. 40% qualifying FLUM + 40% interstate frontage now correctly
matches Option 3's 75% test. Old behavior only counted FLUM.

**Live-verified against 100 real Pasco candidates**: 0 of 100 show any
interstate frontage. This is a real, expected result -- ag parcels in
Pasco are all interior/rural, not touching I-75 or I-275. No visible
pathway delta on this data. The fix is a correctness improvement that
applies WHEN a candidate touches an interstate; the code is wired and
tested (measure function runs, returns 0.0 correctly for all 100), but
this sample won't demonstrate a flip. Would need a hand-picked
interstate-adjacent parcel (or a different county's ag data) to see a
real match gain. Left unchanged: candidates in Nassau (I-10), St. Johns
(I-95), Osceola (I-4) don't get this exercised either at the current
`max_candidates=25` default -- also expected, same reason.

### #2 (Option 4 USB perimeter percent) — DONE, live-verified with 8-of-100 real Pasco delta.

`roads_client.py` gained `measure_usb_perimeter_meters()` — queries
Pasco's Rural Area layer for polygons near the candidate, unions them,
subtracts `boundary.intersection(rural_union).length` from
`total_perimeter`. If no rural polygons intersect at all (parcel is
fully outside any mapped rural area), returns full perimeter (100% USB).
`encirclement.determine_pathways()` now takes `usb_perimeter_pct` and
tests `>= 50` for Option 4 instead of the boolean `adjacent_to_usb`.

**Live-verified against 100 real Pasco candidates**: 8 of 100 parcels
LOST a false Option 4 match. All 8 are ag parcels at 100% qualifying
perimeter that sit deep inside Pasco's Northeast Rural Area (USB
perimeter 0-26.5%, well below the 50% statutory threshold). Old
adjacent_to_usb=True (their buffered boundary wasn't wholly within a
single Rural Area polygon, so the touch-test fired); new usb_perimeter_pct
correctly measures how MUCH of the perimeter is outside the Rural Area
and correctly excludes them from Option 4. These parcels still match
Option 1 (they're 100% qualifying), so they're not lost from results
— just no longer bogusly credited with a second pathway they don't
statutorily qualify for.

Sample of the 8 flipped parcels:
```
parcel_id                   ac    pctQ  iPct  uPct  old      new
09-24-17-0000-00600-0000  40.0  100.0% 0.0%  26.5%  [1, 4] -> [1]
16-24-17-0000-01500-0000  39.2  100.0% 0.0%   0.0%  [1, 4] -> [1]
16-24-17-0000-01400-0000  38.5  100.0% 0.0%  26.2%  [1, 4] -> [1]
26-24-17-0000-00400-0070  50.0  100.0% 0.0%   0.0%  [1, 4] -> [1]
26-24-17-0000-00400-0060  23.0  100.0% 0.0%   0.0%  [1, 4] -> [1]
26-24-17-0000-00400-0040  20.1  100.0% 0.0%  16.2%  [1, 4] -> [1]
21-24-17-0000-00200-0000  40.0  100.0% 0.0%  25.2%  [1, 4] -> [1]
```

The reference parcel (`35-24-16-0000-00100-0011`) stays at `[1, 4]` —
it's OUTSIDE the Rural Area, so usb_perimeter_pct=100%, both Options 1
and 4 correctly fire. Same result as prior deploys; sanity intact.

### #3 (population cap, s. 163.3164(4)(f)) — DONE, defensive check enforced.

`county_registry.py` now has `POPULATION_CAP = 1_750_000` at module
scope. Populations refreshed to BEBR April 1, 2024 estimates for the
four pilot counties (source: BEBR "Florida Estimates of Population 2024,"
edr.state.fl.us + bebr.ufl.edu):
- Pasco: 587,000 -> 633,029 (~36% of cap)
- St. Johns: 273,000 -> 331,479 (~19% of cap)
- Nassau: 114,000 -> 103,990 (~6% of cap)
- Osceola: 449,000 -> 451,231 (~26% of cap)

All four are well under the cap; the six other registry counties are
also confirmed under (Hillsborough at ~1.6M is closest to the cap).

`main.py`'s `/api/counties/{county_id}/scan` now returns HTTP 400 with
a clear message when the county's population exceeds POPULATION_CAP,
citing the statute. Not exercised by any current registry entry; strictly
a guard for a future Miami-Dade/Broward addition (or a population update
that pushes an existing county over the threshold).

### End-to-end regression check — all four counties clean.

Ran `run_county_scan(cid, max_candidates=3)` for all four via `.venv312`.
No exceptions, all four return expected results:
- **Pasco**: reference parcel `35-24-16-0000-00100-0011` still [1, 4] —
  consistent with prior deploys. Other two parcels [] at 48% and 24%
  qualifying (also consistent with the ROW-fix session data).
- **Nassau**: 3 parcels at 0% qualifying (Rayonier timberland tracts,
  consistent with STATUS.md's prior notes) — no pathways, correct.
- **St. Johns**: 3 parcels at 0% qualifying — no pathways, correct.
- **Osceola**: 2 parcels at 100% qualifying show [1] only (correct — no
  Rural Area layer for Osceola, so usb_perimeter_pct=0, Option 4
  correctly doesn't fire). Third parcel `012527000000400000` shows the
  Reedy Creek/Disney exclusion flag as expected.
