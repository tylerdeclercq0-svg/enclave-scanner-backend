# Status — 2026-07-03

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

## Blockers (not silently routed around — need a decision)

### Blocker 1: `shapely` fails to build on this machine
`pip install -r requirements.txt` fails building `shapely` from source:
`fatal error C1083: Cannot open include file: 'geos_c.h'`. Root cause:
Python 3.14 here has no prebuilt wheel available for `shapely==2.0.7`,
so pip falls back to a source build, which needs the GEOS C library
headers (not installed, and not trivial to install correctly on
Windows). `requests` installs fine on its own.

Impact: blocks `encirclement.py` (perimeter/adjacency test) and
therefore the full `scan_orchestrator.py` pipeline. `parcel_fetcher.py`
itself does NOT need shapely and works today.

Likely fixes (not yet attempted): pin to an older shapely version with
a Windows cp314 wheel available, or install via conda/a wheel from
Christoph Gohlke's archive, or just run the real deploy target (Linux,
e.g. Render) where prebuilt manylinux wheels exist — this may be a
local-machine-only problem, worth checking there before spending more
effort on the Windows build specifically.

### Blocker 2: Osceola's server has a broken TLS certificate chain
`gis.osceola.org` sends only its leaf certificate, no intermediate —
confirmed via `openssl s_client -showcerts` (1 cert returned, vs. 3 for
St. Johns' host and 2 for ArcGIS Online). The intermediate is Entrust's
"Entrust DV TLS Issuing RSA CA 2".

This is why it worked during Step 3 testing via `curl` on Windows: curl
uses Windows' schannel, which automatically fetches missing
intermediates via the certificate's AIA (Authority Information Access)
extension. Python's `requests`/`certifi` uses a static trust bundle with
no such fallback, so it fails with
`SSLCertVerificationError: unable to get local issuer certificate`. This
is a REAL SERVER MISCONFIGURATION on Osceola's end, not an environment
quirk — it will also fail on a Linux production server (e.g. Render),
not just here.

Did NOT disable SSL verification to route around this — that's a real
security tradeoff, not a call to make unilaterally.

Likely fixes (not yet attempted): (a) fetch the missing Entrust
intermediate cert and pass it to `requests` via a custom CA bundle
(`verify=` pointing at certifi's bundle + the one extra cert) — a real
fix, not a security downgrade, since full chain validation still
happens; or (b) file a request with Osceola County GIS to fix their
server's cert chain (out of our control/timeline).

## Exact next step

1. Decide how to handle Blocker 1 (shapely) — try an older
   pinned version, or defer full pipeline testing to the real Linux
   deploy target.
2. Decide how to handle Blocker 2 (Osceola cert chain) — build the
   custom CA bundle fix, or proceed with three of four counties
   working and flag Osceola as pending.
3. Once shapely is available, run `run_county_scan()` end-to-end for at
   least Pasco (already fully confirmed at the parcel_fetcher layer) and
   see what breaks in `encirclement.py`/`exclusions.py`/`scoring.py` —
   none of those have been exercised against live data yet.
4. Then Step 5 (wire the `web/` dashboard to real endpoints) is still
   fully unstarted.

## Known gaps (already flagged, still true, not addressed this pass)

5-year continuous ag-use history, Wekiva/Everglades exclusion
boundaries, public-services availability, single-owner-as-of-1/1/2025
(vs. current owner of record), and the unincorporated-status hard
filter (data now exists for Osceola/`Jurisdiction` and is surfaced as a
manual-review flag in `scan_orchestrator.py`, but is NOT yet enforced as
an automatic exclusion for any county).
