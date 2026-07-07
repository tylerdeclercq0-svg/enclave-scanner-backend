# Roadmap

Prioritized checklist of outstanding work for the FL agricultural enclave
scanner (SB 686 / Ch. 2026-34). This file is the index; `STATUS.md` is the
technical state-of-the-world. Read both at the start of every session.

For every item below, **full detailed instructions will be provided in a
separate prompt when the item is actively being worked** -- this file is
the index/checklist, not the full spec.

## Priority order

- [x] **1. SECURITY -- lock down `/api/debug/acs-probe`** *(done 2026-07-06)*
  Gated behind a `DEBUG_API_KEY` shared-secret via `_require_debug_key()`
  in `app/main.py`. Accepts the secret via `X-Debug-Key` header (preferred)
  or `?debug_key=` query param, uses `hmac.compare_digest` for timing-safe
  compare, and **fails closed with 503** if `DEBUG_API_KEY` isn't set on
  the server (a missing env var cannot accidentally re-expose the
  endpoint). Verified locally across all six cases: correct key via header
  or query -> 200; missing/wrong key -> 401; env var unset -> 503
  regardless of what the caller sends. Set `DEBUG_API_KEY` on Render the
  same way `CENSUS_API_KEY` is set before the next deploy.

- [x] **2. EXPORT REFACTOR -- generalize `exportDiligenceTracker`** *(done 2026-07-06)*
  `exportDiligenceTracker(parcels)` in `web/index.html` now takes the
  parcels array as a parameter -- the internal
  `results.filter(r => selectedParcels.has(rowKey(r)))` was moved to the
  current-scan button's click handler. Verified via a preview_eval-driven
  fetch-intercept that the POST payload is identical to pre-refactor: all
  26 row keys, values passthrough, row order preserved; empty-array and
  `null` inputs both correctly early-return with no fetch fired. Future
  Master DB list view (item 6) can attach its own listener that passes
  whatever's checked there.

- [x] **3. FOUNDATION REWORK, PART 1 -- Master DB list becomes the primary landing view** *(done 2026-07-06)*
  Added a two-tab primary nav ("Property Database" / "Data Collection")
  above the masthead-styled header. Property Database is the default
  active tab and lands users on the ranked list -- the DB panel used to
  live inside an overlay modal opened via a masthead link; that modal
  wrapper is gone and its contents render inline under
  `#databaseView`. Data Collection holds the existing 4-step scan
  wizard (Back/Continue nav, stepbar, all four step-contents) untouched
  behind the tab; nothing was removed. Redundant `dbMapLink` masthead
  button dropped since the tab supersedes it. State survives tab
  switches -- `_dbInitialized` flag prevents re-fetching
  `/api/property-db/all` when returning to the DB tab. Verified with
  10 mock parcels across pasco/nassau/st_johns + 8 counties (4 live,
  4 coming-soon): landing shows ranked list first, Data Collection tab
  shows full wizard with county cards, switching preserves state.

- [x] **4. FOUNDATION REWORK, PART 2 -- hide Excluded-tier parcels by default** *(done 2026-07-06)*
  Master DB list view now filters `tier === 'excluded'` rows out of the
  default view. A "Show excluded" checkbox in the list controls reveals
  them; explicitly selecting "Excluded" in the Tier filter dropdown also
  overrides the hide so the user can pull them up without touching the
  toggle. Summary line shows a clay-colored "N excluded hidden" note
  whose count is filter-aware -- if county=pasco is active, only pasco
  excluded rows count toward that note, matching what the toggle would
  reveal. Verified in preview across 5 cases (default hides / toggle
  reveals all / tier-filter overrides / county+hide / county+show).
  Export from item 2 automatically respects the current filter since
  it reads from `_dbListFilteredRows()`.

- [x] **5. FOUNDATION REWORK, PART 3 -- metro-proximity signal** *(done 2026-07-06)*
  New `app/metro_proximity.py` module: pulls 955 FL Census places from
  TIGERweb (Incorporated Places layer 28 + CDPs layer 30) + ACS 2023
  5-year for population + median HH income, and computes the transparent
  "metro pull" score via `log10((pop * income) / (distance + 1))`.
  Nearest-place lookup uses real haversine distance from the parcel
  centroid over every FL place -- never "whichever place came back
  first from a spatial query" (the same class of bug caught last session
  for county attribution, `find_bg_containing_point` fix). All raw inputs
  to the score are stored on `ScanResultRow` alongside the score, so
  every ranking is auditable. Wired into `scan_orchestrator.run_county_scan`
  via `_load_fl_places_safely` (returns `[]` on any Census/TIGERweb
  failure or missing `CENSUS_API_KEY`, so metro proximity being down
  never breaks a scan). Live-verified: reference sanity check hits
  Kissimmee (score 9.31) as Osceola's dominant metro, Land O' Lakes
  (8.74) leads for Pasco, St. Augustine (8.64) for St. Johns; 19 real
  rural parcels across Pasco + Nassau scored in a coherent 7.0-7.4
  range appropriate for their small nearest CDPs. Score is NEVER
  blended into `tier` or `attractiveness_score` -- purely a secondary
  sort key inside a tier (see item 6 for the frontend wiring).

- [x] **6. FOUNDATION REWORK, PART 4 -- the actual list view** *(done 2026-07-06)*
  Core list view lives in the Property Database landing tab alongside
  the map, with a Map / List toggle at the top and shared
  `_dbAllParcels` data source. `DB_LIST_COLUMNS` has 14 columns:
  tier, score, driving pathway(s), county, parcel_id, acres, owner,
  ZIP, last scanned, plus the item-5 metro extension (Metro / Miles /
  Metro pop / Metro inc / Metro pull). Default sort tier asc +
  `metro_pull_score` desc; rows with a null metro score sort last
  within their tier so they don't crowd out real matches. All headers
  click-to-sort with direction toggle. Filter dropdowns for county,
  tier, and metro (metro dropdown populated dynamically from whatever
  metros the loaded data actually references, same pattern as
  county). Excluded rows hidden by default per item 4. Per-row +
  select-all checkboxes; "Export selected (.xlsx)" reuses
  `exportDiligenceTracker(parcels)` from item 2. Verified in preview:
  Confirmed-tier tie-break by metro pull works (Tampa-adjacent 9.402
  above Land-O'-Lakes-adjacent 8.739), null-metro rows sort last
  within tier as designed.

- [x] **7. SCALE-UP, PHASE 1 -- statewide reconnaissance across 65 eligible counties** *(done 2026-07-06)*
  Cheap AGOL-only existence check ran against all 58 remaining counties
  (65 statute-eligible minus 7 already `confirmed_live=True` in
  `county_registry.py`). Method: ArcGIS Online item search with a tight
  match rule (title must contain the county name AND "parcel"/"cadastr").
  Buckets: **10 live** (Citrus, Collier, Gadsden, Glades, Hendry, Lee,
  Leon, Okeechobee, Pinellas, Wakulla; Gadsden + Wakulla share one
  Leon-hosted layer so it's one integration), **21 unclear**, **27
  none**. Full triage list committed at
  [`scripts/phase1_recon_results.md`](scripts/phase1_recon_results.md);
  the recon script itself is at
  [`scripts/phase1_recon.py`](scripts/phase1_recon.py) for re-runs.
  **Known limitation of this method:** many populous FL counties host
  their own on-prem ArcGIS Server or Property Appraiser subdomain not
  indexed on AGOL under standard parcel titles, so "none" here is
  *not* equivalent to "no service exists" -- see item 9's re-check
  list.
  Cheap reconnaissance pass across all 65 statute-eligible FL counties
  (all 67 minus Miami-Dade and Broward, which exceed the 1.75M population
  cap enforced per `s. 163.3164(4)(f)` in `main.py`'s
  `/api/counties/{id}/scan` guard). For each county, check whether a
  usable GIS parcel service exists at all -- ArcGIS FeatureServer /
  MapServer discoverable via ArcGIS Online item search, county open-data
  portal, or direct county-GIS URL -- and record the endpoint, use-code
  field name, and acreage field name. Output is a triage list, not a
  full verification -- the point is to know which counties are cheap vs.
  expensive to onboard before spending real describe-layer / live-query
  effort per county. **Status: not started.** Full detailed instructions
  will be provided in a separate prompt when this item is actively being
  worked.

- [x] **8. SCALE-UP, PHASE 2 -- reorder the per-parcel pipeline for early exclusion gating** *(done 2026-07-06)*
  Partial reorder shipped, with a full honest report. `exclusions.check_exclusions`
  (Wekiva / Everglades / ACSC -- Fix-A-cached statewide, per-parcel cost
  is a local Shapely intersect) now runs BEFORE the concurrent I/O
  phase; any parcel hitting one skips FLUM neighbor fetch + interstate/USB
  adjacency + follow-ups + encirclement entirely (~1.5s/parcel per hit).
  New named block `early_exclusion_gate` positions future hard-fail
  checks (conservation easements, military buffers if data ever
  surfaces) obviously.

  **`unincorporated_check` was NOT moved to the early gate** after
  measurement showed it was empirically wrong: the check's ~500 ms
  network RTT was free-riding on FLUM's ~800 ms concurrent wait, so
  serializing it exposed a real cost with no offsetting benefit.
  Documented inline in the code for future engineers.

  **Measurement (same 25-parcel Pasco batch used for Fixes A/B/C):**
  - BEFORE (baseline):    45.09s (1.80s/parcel)
  - FULL REORDER attempt: 56.10s (2.24s/parcel) -- **-24% regression**
  - PARTIAL (shipped):    45.97s (1.84s/parcel) -- **within Render/
                                                    ArcGIS variance**

  Correctness verified in both attempts: same 25 parcels, exact same
  tier distribution, zero field diffs on tier / score /
  pct_perimeter_qualifying / likely_pathways / exclusion_flags.

  **Honest assessment:** no measured wall-clock win on Pasco because
  the early-exclusion rate on that batch is 0/25 (Pasco isn't in any
  of the statewide exclusion zones, and no ag parcels landed in an
  incorporated city). The reorder is architectural positioning for
  future scans hitting Wekiva (Central FL) / Everglades (South FL) /
  ACSC (Green Swamp, Big Cypress, Apalachicola, Florida Keys) -- not
  a measured win today. Item 13's real-data pass will show the
  actual aggregate exclusion rate across 30 counties; if it's still
  low, this reorder was positioning-not-a-win and that's fine (Tyler's
  own directive).

  Recon script at [`scripts/phase2_profile.py`](scripts/phase2_profile.py)
  for future re-profiling.

- [~] **9. SCALE-UP, PHASE 3 -- prioritized full verification to >=30 confirmed-live counties** *(Wave 1 done 2026-07-06, 3/10 wired; further waves needed to reach the >=30 goal)*
  Using item 7's triage list
  ([`scripts/phase1_recon_results.md`](scripts/phase1_recon_results.md)),
  work through counties in priority order to reach at least 30
  confirmed-live counties. Priority uses the same growth-rate /
  median-income prioritization already used for selecting the original
  four pilot counties (Pasco, Nassau, St. Johns, Osceola). Each new
  county needs the full ground-truthing pass documented in STATUS.md:
  `describe_layer` verification, real field-name confirmation, live
  agricultural-classification test, live end-to-end
  `fetch_candidate_parcels`, and `confirmed_live=True` in
  `county_registry.py`.

  **CRITICAL: item 7's "none" bucket is NOT authoritative.** Phase 1's
  AGOL-only search misses any county that hosts its own on-prem ArcGIS
  Server or publishes through a Property Appraiser subdomain not
  indexed on AGOL. Before writing off any "none" or "unclear" county
  from item 7's list, specifically re-check these five known-significant
  counties via direct Property Appraiser sites and known county GIS
  URL patterns: **Alachua, Marion, Polk, Duval, and Monroe** -- these
  are all large, active-GIS counties almost certainly miscategorized by
  Phase 1's method, and writing them off from the Phase 1 baseline
  without a direct-URL re-check would be a real oversight. Same
  discipline (Property Appraiser site + direct county GIS URL check)
  should be applied to any other "none" county before it's dropped from
  Phase 3's scope.

  **Blocked on item 7** (need the triage list to pick from) -- now
  complete.

  **Wave 1 outcome (2026-07-06):** worked through the 10 counties
  Phase 1 confidently marked "live." Only **3/10 turned out viable**
  via Phase 1's URLs -- the others have parcel services that lack
  critical fields (use code, acreage) or are misindexed:

  | County | Outcome | Reason |
  |---|---|---|
  | **Lee** | ✓ WIRED + end-to-end scan verified | DORCODE 2-digit range, GISACRES, O_NAME; FLUM ag_flu_values 'Coastal Rural' (Wave-1 best-guess) |
  | **Leon** | ✓ WIRED + end-to-end scan verified | PROP_USE 4-digit CAST-to-int, CALC_ACREA, OWNER1/OWNER2; FLUM 'AG' (highest-confidence FLUM value of the wave) |
  | **Citrus** | ✓ WIRED + end-to-end scan verified | LUC 4-digit CAST-to-int + blank-string guard, OWN1/OWN2, geometry-computed acreage; FLUM 'RUR' (Wave-1 best-guess) |
  | Collier | not viable via Phase 1 URLs | Both "Parcels" (10-field join view) and "Simplified Parcels" (4 fields) lack a use-code field |
  | Glades | not viable via Phase 1 URLs | 6-field layer, only PARCELNO |
  | Hendry | not viable via Phase 1 URLs | 9-field layer, no use code, no acreage |
  | Pinellas | not viable via Phase 1 URLs | Both AGOL and egis.pinellas.gov URLs are SURVEY PLAN datasets (PLANID/ACCURACY/MISCLOSERATIO), not parcels |
  | Gadsden + Wakulla | not viable via Phase 1 URLs | 8-field Leon-hosted overlay (TAXID/OWNER/ADDRESS/JURISDICTION), no use code |
  | Okeechobee | not viable via Phase 1 URLs | 91 fields but all `gis_int_*` columns are empty strings -- broken join dump |

  Non-viable != impossible: these counties may still have real GIS
  behind a Property Appraiser subdomain not indexed on AGOL under the
  search terms Phase 1 tried (matches Phase 1's own known limitation).
  Adding any of them requires a per-county deep-dive.

  Investigation script:
  [`scripts/phase3_investigate.py`](scripts/phase3_investigate.py).

  **Wave 1 caveats worth flagging for a future refinement pass:**
  - FLUM `agricultural_flu_values` for Lee ('Coastal Rural') and Citrus
    ('RUR') are best-guesses from a 500-row distinct-values sample.
    Both need a scan-quality check before item 13's real-data run.
  - None of the 3 wired counties has an automated unincorporated check;
    all three left at `unincorporated_check=manual_only`.
  - Leon's SALEDTE_S1/SALEDTE_S2 encoding wasn't decoded in Wave 1;
    `sale_date_encoding=None` so the post-1/1/2025 flag can't fire.

  **Wave 1 validation pass (2026-07-06):** Lee and Citrus's FLUM
  `agricultural_flu_values` guesses were live-tested against known ag
  parcels' FLUM-at-centroid values -- both were wrong:
  - Lee: Wave-1 guess `('Coastal Rural',)` REFINED to `('Rural',
    'Coastal Rural', 'Rural Community Preserve',
    'Density Reduction/Groundwater Resource', 'DRGR', 'Lee County
    FLUM: DRGR', 'Open Lands')` after a full paginated distinct-values
    pull found 90+ FLU codes and 3-parcel test showed real ag parcels
    sit in Rural + DRGR (not just Coastal Rural).
  - Citrus: `('RUR',)` REFINED to `('AGR', 'RUR')` -- 'AGR' (40x in
    full distinct pull) is the primary ag designation and was missed.
  - Lee unincorporated_check: WIRED to `city_limits_layer_join` via
    the Lee County Planning Tool's Municipal Boundaries layer 4
    (CityName field, real values 'City of Cape Coral', etc.).
    First post-fix scan proved it works: TAMIAMI DEL PRADO ACQ LLC
    (41ac) correctly flagged as inside City of Cape Coral -> excluded
    (silently scanned as a candidate in Wave 1).
  - Citrus unincorporated_check: WIRED to `city_limits_layer_join`
    via Citrus's CityBoundaries layer 9 (CORPNAME field, real values
    'CRYSTAL RIVER', 'INVERNESS').
  - Leon unincorporated_check: stays manual_only. TAXDIST field is
    uniformly '1' across a 2000-row sample; AGOL search didn't
    surface a Tallahassee city-limits FeatureServer. Explicitly
    deferred with reason.
  - Leon SALEDTE_S1 DECODED as MMYYYY 6-char string (samples:
    '042025' = Apr 2025). New `mmyyyy_string` sale_date_encoding
    added to statutory_checks.py. Live-verified: POWERHOUSE INC's
    parcel (SALEDTE_S1='042025') correctly returns
    sold_since_2025=True.

  **Wave 2 (2026-07-06):** Probed the 10 Wave-2 counties
  Tyler flagged via county Property Appraiser + GIS URL patterns +
  AGOL owner search. **Actual conversion: 2 of 10 confirmed viable**
  (Alachua, Manatee), 8 not viable via best-effort recon:

  | County | Outcome | Detail |
  |---|---|---|
  | Alachua | ✓ viable (identified, NOT yet wired) | AlachuaCountyGIS/Parcels35_view: puse code (5500/6500/5900 confirmed ag), PUSECAT='Agricultural', acres Double, firstName1 owner. FLUM candidate `Future_Land_Use04` needs layer re-selection. |
  | Manatee | ✓ viable (identified, NOT yet wired) | mymanatee.org opendata/General: LUC code (5000/6000 confirmed ag), LUC_DESCRIPTION, ACRES String, OWNER+SECONDARY_OWNER, FUTURE_LAND_USE on the parcel layer (AG-R = ag) -- unusual pattern that deviates from pilot counties. |
  | Palm Beach | ✗ not viable via cheap recon | No official parcel layer surfaced; hits are third-party republishes |
  | Sarasota | ✗ not viable via cheap recon | Same |
  | Orange | ✗ not viable via cheap recon | Top AGOL hits are NC Orange County (Chapel Hill) and VT Orange County -- classic false positives |
  | Seminole | ✗ not viable via cheap recon | Top hits are OK Seminole/tribal data |
  | Marion | ✗ not viable via cheap recon | TN + WV + IN Marion County false positives |
  | Polk | ✗ not viable via cheap recon | MN Polk hits; FL Polk PA subdomain didn't respond to any tried URL pattern |
  | Duval | ✗ not viable via cheap recon | Only informal Jax University-owned reshares |
  | Monroe | ✗ not viable via cheap recon | MonroeCountyGIS org exists (uncertain state) but Current_Parcels layer has only 8 usable fields, no obvious use-code field |

  **Wave 2 conversion rate: 2/10** -- lower than Wave 1's 3/10.
  Property Appraiser direct URLs mostly resolved to 404s or SSL
  failures. Getting any of the 8 non-viable counties requires
  per-county targeted investigation.

  Recon script for Wave 2 re-runs:
  [`scripts/phase3_wave2_recon.py`](scripts/phase3_wave2_recon.py).

  **Deliberately not wired under time pressure:** Alachua and Manatee
  are confirmed viable but need dedicated attention -- Alachua's FLUM
  layer needs re-picked (initial candidate is "Urban Service Area"
  not the actual FLUM), and Manatee's FLU-on-parcel-layer pattern
  needs the scan_orchestrator to be validated against a non-
  separate-FLUM-service county. Recommend a focused Wave 2b prompt
  to wire these two properly rather than shipping them with the
  same shortcut that caused Wave 1's FLUM ag_values gap.

  **Status: 3 wired (Wave 1), 2 confirmed viable pending wire-in,
  15 non-viable via cheap recon.** Total real wire-ins: 3/30.
  Reaching >=30 is a long road via cheap recon; each new county will
  require dedicated per-county effort from here.

  **Wave 2 followup with Tyler's real leads (2026-07-06):** Tyler
  did a real web search outside Claude's tools and surfaced four
  leads that Claude's blind-URL + AGOL search had missed. Findings:

  **MASSIVE UNLOCK: SWFWMD's shared parcel_search MapServer.** The
  URL Tyler gave for Marion
  (`www25.swfwmd.state.fl.us/arcgis12/rest/services/BaseVector/parcel_search/MapServer/11`)
  is not just a Marion layer -- **the same MapServer hosts 16
  county parcel layers as separate layer IDs**, all with an
  IDENTICAL 95-field schema (confirmed live: Marion layer 11,
  Sarasota layer 15, Manatee layer 10, Polk layer 14 all show the
  same field set). Layers cover Charlotte (1), Citrus (2), DeSoto
  (3), Hardee (4), Hernando (5), Highlands (6), Hillsborough (7),
  Lake (8), Levy (9), Manatee (10), Marion (11), Pasco (12),
  Pinellas (13), Polk (14), Sarasota (15), Sumter (16). Same
  integration effort unlocks all 16 (like Leon/Gadsden/Wakulla
  pattern, but 16-way). Standardized SWFWMD fields:
    - `PARUSECODE` (String, DOR 3-char, e.g. '069' ORNAMENTALS ag)
    - `AREANO` (Double, polygon area in acres -- use this NOT
      `ACRES` which is often null)
    - `OWNNAME` (String, full owner)
    - `PARNO` (String, parcel ID)
    - `SALE1_YEAR` (SmallInteger, sale year -- clean year_only encoding)
    - `PALINK` (URL to county PA record)
  Real-parcel test on Marion: 6 ag parcels with PARUSECODE='069'
  (STEPHEN MCDONALD GRASSING LLC) returned cleanly. **Caveat: the
  SWFWMD service is documented as available 6 AM - 10 PM daily only.**
  A production scan running outside those hours will fail -- would
  need job-scheduling guards for item 13.

  **Duval / Jacksonville consolidated gov confirmed.**
  `maps.coj.net/coj/rest/services/CityBiz/Parcels/MapServer/0`
  is real, 74 fields, description "Duval County Parcels", official
  City of Jacksonville source. Real fields: `ACRES` (Double, always
  populated), `LNAMEOWNER` (String), `PUSE` (String, 4-char DOR
  code, ag range '5000'-'6999' via CAST-to-int matches the Osceola/
  Leon pattern), `SALESLDD`/`SALESLMM`/`SALESLYY` (ymd_ints
  encoding), `RE` (parcel ID). Real ag samples returned:
  BIG CREEK TIMBER LLC 407ac PUSE='5600' (Timberland), PATEL ASSET
  HOLDINGS 94ac, SALLETTE LIVING TRUST 27ac.

  **Polk County Hub site (Tyler's link):** covered by SWFWMD
  layer 14 -- Hub browsing not needed.

  **Monroe County qPublic**: confirmed different platform
  (qpublic.schneidercorp.com is a Schneider Corp interactive
  application, no public ArcGIS REST endpoint). Would require
  either a Schneider Corp API contract or web scraping of the
  interactive UI. Deprioritized as a fundamentally different
  integration.

  **FLUM discovery challenge (blocker on wiring):** the SWFWMD +
  Duval parcel wins do NOT come with matching FLUM layers. Wave 1
  taught us that FLUM `agricultural_flu_values` MUST be validated
  against known ag parcels via FLUM-at-centroid tests -- shortcuts
  cost a whole validation pass. Tried multiple candidates:
  - Marion FL FLUM: only third-party RDG-hosted layers surface on
    AGOL. Marion County's OWN AGOL org (`Marion_County`) turned out
    to be **Oregon's** Marion County (Willamette Greenway, Measure
    37/49) -- a classic homonym trap the tool bit on before.
  - Duval FLUM: `Jacksonville_FLU` on AGOL requires an auth token
    ("Token Required" HTTP 499). Other Jax hits are UGB or CWPP
    datasets, not FLUM.
  - Sarasota/Manatee/Polk FLUM: not surfaced during this pass.

  **Deliberately not wired in this session, per Wave 1's discipline
  lesson.** Wiring Marion/Duval with best-guess FLUM would repeat
  Wave 1's ag_flu_values gap. Confidence-preserving next step:

  **Wave 2b prompt should focus on FLUM discovery per county.**
  For each of Marion / Duval / Sarasota / Manatee / Polk: find the
  county's OFFICIAL comprehensive-plan FLUM layer (not an AGOL
  third-party republish), validate with a FLUM-at-centroid test
  against 3 known ag parcels from that county, then wire. Parcel
  infrastructure is already known -- half the work is done.

  Investigation script from this pass:
  [`scripts/phase3_wave2_recon.py`](scripts/phase3_wave2_recon.py).

  **Wave 2b outcome (2026-07-06):** two additions closed out, three
  still blocked.

  FIRST -- SWFWMD availability window now a REAL, enforced constraint
  (not just a doc note). New `app/service_windows.py` module. Sync
  /scan + /coverage/advance + background /scan-entire-county all
  refuse to start outside 6 AM-10 PM Eastern for SWFWMD-sourced
  counties (503 with next-window-open time). A running background
  job that crosses INTO blackout mid-run transitions to
  `paused_awaiting_window` and sleeps in-thread until the window
  reopens. On process restart, `mark_interrupted_at_startup` picks
  up paused jobs whose window has since reopened and spawns fresh
  worker threads. `CountyEndpoint.parcel_source` field carries the
  source identifier + a concentration-risk note (every SWFWMD-
  sourced county shares one upstream mirror -- a schema drift or
  outage there hits all of them at once, unlike direct-county
  sources which each have their own failure domain).

  SECOND -- FLUM discovery closed for 2 of 5 targeted counties:

  | County | Outcome | Detail |
  |---|---|---|
  | **Sarasota** | ✓ WIRED + verified live | Official FLUM at `ags3.scgov.net/server/rest/services/Hosted/FutureLandUse` (scgov.net = Sarasota County Gov). `flucode` field, ag values `RURAL` + `SRURAL`. Test parcels (BYRD LARRY DOR-062 Pasture) correctly sit in `MODR` = candidate enclave. |
  | **Manatee** | ✓ WIRED + verified live | Official FLUM at `mymanatee.org/gisits/rest/services/opendata/Planning/FeatureServer/1`. `FLULABEL` field (FLUTYPE is null on some rows), ag value `AG-R`. Test parcels (DAKIN 340ac, MANNING 279/197ac) all correctly in FLULABEL='AG-R'. MANNING parcels correctly flagged sold_since_2025=True via SALE1_YEAR year_only encoding. |
  | Marion | ✗ FLUM still blocked | Marion FL has no official AGOL org (the `Marion_County` org that surfaces is Marion COUNTY OREGON -- Willamette Greenway, Measure 37/49). No FLUM found on official county sites. Parcel infrastructure via SWFWMD layer 11 is confirmed and ready. |
  | Polk | ✗ FLUM still blocked | Polk Hub site (polk-county-geoportal-open-data-polk-bocc-gis.hub.arcgis.com) exists but Hub API queries returned empty. Parcel infrastructure via SWFWMD layer 14 is confirmed and ready. |
  | Duval | ✗ FLUM inaccessible | coj.net has 33 folders none of which host a land use / zoning / planning service. `Jacksonville_FLU` on AGOL requires auth token (HTTP 499 Token Required). Parcel infrastructure via coj.net Parcels layer confirmed and ready. |

  Also confirmed by wire-up: SWFWMD's 95-field schema uniformity means
  ONE `_swfwmd_ag_where` + `_swfwmd_is_agricultural` classifier pair
  in parcel_fetcher serves every SWFWMD-sourced county (registered
  for `sarasota` and `manatee` in this pass). Adding future SWFWMD
  counties is just a new dict entry, not new classifier code.

  **8 more counties on the same SWFWMD schema not yet touched:**
  Charlotte (layer 1), DeSoto (3), Hardee (4), Hernando (5),
  Highlands (6), Lake (8), Levy (9), Sumter (16). Each still needs
  its own official FLUM located + validated per Wave 1 discipline,
  but the parcel-layer half of the work is proven, so this is a
  real sizeable next opportunity. Do NOT start until the current
  batch's blockers close or Tyler explicitly reprioritizes.

  **Wave 2b batch 2 outcome (2026-07-06):** worked through the 8
  remaining SWFWMD-schema counties. Conversion: **2 of 8**.

  | County | Outcome | Detail |
  |---|---|---|
  | **Hardee** | ✓ WIRED + verified live | Official at `gis.hardeecounty.net/arcgis/rest/services/LandUseZoning/MapServer/16` (Future Landuse County). Field `LANDUSECODE`, ag values `AGR` + `RVG` + `CON`. 3 known ag parcels correctly returned tier=unlikely with 0% qualifying (surrounded by ag/rural/conservation, not enclave candidates). |
  | **Charlotte** | ✓ WIRED + verified live | Official at `agis.charlottecountyfl.gov/arcgis/rest/services/Essentials/CCGIS_Web_Layers2022/MapServer/42`. Field `NEWLU`, ag values `Agriculture` + `Rural Estate Residential` + `Rural Community Mixed Use` + `Preservation` + `Resource Conservation`. 3 known ag parcels (VITALE LARRY, ACORN PORT CHARLOTTE, NAJMI PROPERTIES) all returned **tier=confirmed_qualifying at 100% pct** -- exactly the enclave candidates the tool is designed to find (DOR-055 Timberland in Low Density Residential FLUM). |
  | DeSoto | ✗ blocked | Direct county GIS URLs 404 / SSL fail. AGOL FLUM search returned zero viable hits after strict FL filtering. |
  | Hernando | ✗ blocked | Same -- no viable AGOL hits, direct URLs unreachable. |
  | Highlands | ✗ blocked | Same. |
  | Lake | ✗ blocked | AGOL returned only wrong-location results ("District of Lake Country," Douglas NV). No FL Lake County FLUM surfaced despite it being a major county. |
  | Levy | ✗ blocked | Same as DeSoto/Hernando. |
  | Sumter | ✗ blocked | Same. |

  Total Wave 2b wins across both batches: **4 counties** (Sarasota,
  Manatee, Hardee, Charlotte). Total confirmed-live counties: **11**
  of 30 target -- Pasco/Nassau/St. Johns/Osceola (pilots) + Lee/Leon/
  Citrus (Wave 1) + Sarasota/Manatee/Hardee/Charlotte (Wave 2b).

  **6 SWFWMD counties still parcel-ready + FLUM-blocked:** DeSoto,
  Hernando, Highlands, Lake, Levy, Sumter. Same pattern as Marion,
  Polk, Duval from earlier -- parcel infrastructure is proven, FLUM
  needs a different (non-cheap) discovery approach. Reasonable next
  angles when picked up again: (a) each county's Property Appraiser
  site directly, checking for a `<county>pa.org` or similar GIS
  subdomain not tried yet; (b) FGIO / MyRegion state clearinghouses;
  (c) FDOT district FLUM services (D1 covers Charlotte/DeSoto/
  Hardee/Highlands/Lee/Sarasota, D7 covers Citrus/Hernando/Pasco/
  Pinellas -- worth checking for the same "county-per-layer" pattern
  that unlocked SWFWMD).

  **Wave 2b batch 3 outcome -- FDOT district hypothesis + fallbacks
  (2026-07-06):** tested each of the three next-angle approaches
  systematically. Result: **0 additional counties wired.**

  **(a) FDOT district FLUM services** -- confirmed the hypothesis
  correctly: Leon's FLUM is officially served by FDOT District 3
  (owner `matthew.gore_fdot3`, service `D3_FLUM_County/FeatureServer`
  hosting 9 layers, one per D3 county: Bay, Escambia, Gulf, Jackson,
  Leon, Okaloosa, Santa Rosa, Walton, Washington). The pattern is
  real for D3. BUT no equivalent `D1_FLUM_County`, `D5_FLUM_County`,
  or `D7_FLUM_County` service surfaced via: direct URL guessing on
  the same host (`services1.arcgis.com/O1JpcwDW8sjYuddV/`), AGOL
  title searches (`title:"D1_FLUM"` etc), FDOT-owner searches
  (`owner:*_fdot*`), or gis.fdot.gov folder browsing. Individual
  FeatureServers exist on the D3 org for D3 counties (`Walton_
  FutureLandUse`, `Bay_FLUM`, `Escambia_FLUM`, `Jackson_FLUM`,
  `Santa_Rosa_FutureLandUse`) but not for target D1/D5/D7 counties.
  Conclusion: **D3's aggregation was a Panhandle-specific FDOT staff
  initiative**, not a statewide FDOT pattern. Doesn't extend.

  **(b) Property Appraiser subdomain patterns** -- tried 2-4 URL
  patterns per county across the 6 remaining (e.g. `gis.<county>pa.
  com`, `gis.<county>pao.org`, `gis.<county>propertyappraiser.com`,
  county-gov subdomains). **0 of 6 responded** with a valid GIS
  service; all returned DNS failures / SSL errors / 404s. FL PAs
  either don't publish public ArcGIS REST endpoints under naming
  patterns discoverable this way, or run on non-ArcGIS platforms
  (Monroe = qPublic pattern already documented).

  **(c) FGIO / statewide FLUM aggregator** -- FloridaGIO owner
  publishes `Florida_Statewide_Cadastral` (already known, parcel-
  level not FLU) and `Florida_Statewide_Parcel_Centroid_Version`.
  No statewide FLUM aggregator surfaced -- suggests it doesn't exist
  or isn't published to AGOL.

  **Final Wave 2b state: 11 confirmed-live counties** (Pasco, Nassau,
  St. Johns, Osceola + Wave 1 Lee, Leon, Citrus + Wave 2b Sarasota,
  Manatee, Hardee, Charlotte). **9 counties parcel-ready +
  FLUM-blocked** (Marion, Polk, Duval, DeSoto, Hernando, Highlands,
  Lake, Levy, Sumter). **Reaching >=30 target requires a
  fundamentally different approach for the 19-county gap** -- most
  likely per-county interactive investigation (browsing each PA's
  actual site, not URL-pattern guessing) rather than another
  automatable search pass. Cheap search has been exhausted.

  **Wave 2b batch 4 -- Marion/Duval Hub-portal + application-URL leads
  (2026-07-06):** Tyler surfaced two real web-search leads for
  counties previously reported blocked. Result: **1 additional wired.**

  **Marion** -- Tyler's lead
  (data-marioncountyfl.opendata.arcgis.com/datasets/futurelanduse)
  panned out perfectly. Marion's OWN opendata Hub exposes a real DCAT
  feed at `/api/feed/dcat-us/1.1.json` which surfaced the underlying
  FeatureServer at
  `services1.arcgis.com/oMGpBoZpy1Db2sAl/FutureLandUse/FeatureServer/0`.
  This is Marion FL's real AGOL org -- different from the "Marion_
  County" org that turned out to be Marion County OREGON. Real fields
  `LANDUSECOD` (Land Use Code, mostly 'RL' base zoning) + `GS_FLUM`
  (Growth Services FLUM, the granular current designation, distinct
  values: RL, P, LR, MR, COM, EC, PR, HR, M). ag_flu_values=('RL',)
  validated via FLUM-at-parcel-centroid on 3 known SWFWMD ag parcels
  (STEPHEN MCDONALD GRASSING LLC, PARUSECODE=069 Ornamentals, 21-28ac):
  all 3 correctly in GS_FLUM='RL'. Live scan verified end-to-end.
  **Marion WIRED.**

  **Duval** -- Tyler's lead
  (maps.coj.net/duvalproperty/) investigated. The application page
  homepage HTML contains a `Download Land Use layer` link pointing to
  `mapstest.coj.net/publicdata/landuse.zip` -- Duval's FLU is
  published as a **downloadable ZIP shapefile**, NOT a live REST
  endpoint. `mapstest.coj.net` doesn't resolve externally
  (consistent with the "scheduled maintenance" note Tyler mentioned).
  The duvalproperty app's JS (`js/homePage.js`, `js/misc.js`, etc.)
  only exposes imagery URLs, no FLU MapServer. Coj.net's 33 REST
  folders + guessed URL variants + alternative subdomains all
  returned nothing FLU-related. **Duval FLU is available for USE
  but requires a fundamentally different integration** (shapefile
  ingestion + either self-hosting a REST service or in-memory
  spatial index). Not tractable in this session's scope. Reported
  honestly rather than substituting a lesser source silently.

  **State after batch 4: 12 confirmed-live counties** (Pilots +
  Wave 1 + Wave 2b batch 1 + Wave 2b batch 2 + Marion). **8 counties
  parcel-ready + FLUM-blocked** (Polk, Duval, DeSoto, Hernando,
  Highlands, Lake, Levy, Sumter). Duval specifically has KNOWN FLU
  DATA -- just as a ZIP not a live service -- so it's the strongest
  candidate for future work if shapefile ingestion is scoped in.

- [x] **10. VERIFY BACKGROUND SCAN JOB POST-RESTRUCTURING** *(done 2026-07-06, piggybacked on item 5's verification)*
  Kicked off a real "Scan entire county" background job on Nassau via
  `POST /api/coverage/nassau/scan-entire-county` with the deployed
  post-restructuring build. Job transitioned cleanly queued -> running
  -> terminal over ~90 seconds: `batches_this_run` ticked 1 -> 2 -> 4,
  `processed_this_run` 5 -> 10 -> 14, `current_zcta`/`last_updated_at`
  updated at each checkpoint. Terminal state was `error` with an
  informative message ("Advance for ZCTA 31537 returned 0 rows but the
  ZCTA still shows 4 candidates remaining. Filters may be too
  restrictive; loosen them and resume.") -- a data/filter mismatch
  condition, not a code regression from the tab restructuring (which
  only touched HTML/CSS/JS, not the server-side runner or ledger).
  Backend contract for polling and persistence holds: 14 real Nassau
  parcels landed in the master DB via `save_parcel_results` and are
  visible through `/api/property-db/all`.

- [x] **11. INVESTIGATE + FIX ZCTA CANDIDATE-COUNT-VS-ACTUAL-QUERY MISMATCH** *(done 2026-07-06)*
  Root cause: query construction divergence, NOT stale count and NOT
  "filters too restrictive" (that error message was itself a symptom).
  The ledger's `total_candidates` was computed by
  `zcta_client.count_parcels_in_zcta` with only the server-side ag
  WHERE clause + spatial intersect; `parcel_fetcher.fetch_candidate_parcels`
  then applied client-side filters (`min_acreage`/`max_acreage`,
  `is_agricultural` re-check, `require_single_owner`) that silently
  dropped many. Any parcel matching the WHERE but failing a client-side
  filter inflated total_candidates without ever being fetch-able --
  ledger's "remaining" number never hit zero, background jobs
  terminated with "0 rows but N candidates remaining."

  **Systemic, not Nassau-specific.** Audit across 12 sampled ZCTAs
  (top-3 by area in each pilot county) found **52.3% of the OLD total
  was spurious.** Example divergences:
  - Nassau 31537: 18 OLD -> 14 NEW (4 parcels below the 20-acre floor)
  - Nassau 32046: 1796 -> 846 (dropped 950, 53%)
  - Pasco 33523: 1328 -> 629 (dropped 699, 53%)
  - Pasco 33597: 28 -> 33 (**NEW is +5** -- OLD *under*-counted here
    because the client-side `is_agricultural` re-check accepts parcels
    the server WHERE doesn't return; divergence works both directions)

  **Fix:** new `parcel_fetcher.count_matching_candidates()` calls
  `fetch_candidate_parcels` internally so count and fetch share the
  identical code path -- divergence impossible by construction.
  `main.py` (`/coverage/{id}/advance`) and `background_jobs.py`
  (`scan-entire-county`) both switched to it. Both call sites also add
  a self-heal: on advance-returns-zero-rows-with-remaining, re-verify
  the total via the same helper and update the ledger. Ledgers
  persisted before this commit auto-correct on the next advance touch;
  the "0 rows / N remaining" error now only surfaces if divergence
  persists AFTER the self-heal (i.e. a genuinely-unexpected condition,
  no longer a spurious ledger-accounting misfire).

  **Live-verified:** kicked off a Nassau `scan-entire-county` job with
  the same params as item 10's failing run. ZCTA 31537 now shows
  `total_candidates=14, processed=14, complete=True` and the job
  advanced cleanly through 15 ZCTAs (4 complete + 1 in progress + 91
  parcels processed) before I cancelled it. Pasco ZCTA 33523's new
  `total_candidates=629` on a fresh advance confirms the fix runs
  server-side (old bug would have shown 1328).

  **No pre-fix "complete" state anywhere to reset.** The only ledger
  persistence surface is `data/coverage_ledger.json` on Render's
  ephemeral Starter-tier filesystem. No git-tracked ledger files (`data/`
  is fully `.gitignore`d), no external DB backend in `requirements.txt`,
  no persistent-disk mount in Procfile. Render instance restarts (not
  just redeploys) wipe the ledger -- confirmed live: the 4-complete
  Nassau state I created during the fix verification was already gone
  by the time I re-checked ~15 min later. So any ZCTA that had been
  falsely marked complete under the OLD undercounting logic is
  guaranteed already gone.

- [x] **12. DURABLE PERSISTENCE FOR THE COVERAGE LEDGER + PROPERTY DATABASE** *(done 2026-07-06)*
  Discovered during item 11 verification: the coverage ledger and
  property database persist only to `data/coverage_ledger.json` on
  Render's Starter-tier ephemeral filesystem, which gets wiped not
  just on redeploys but on any instance restart -- and Render provides
  no notification when a restart happens. Confirmed live: the 4-complete
  Nassau ZCTA state populated during item 11's verification was gone
  ~15 min later without a redeploy. **Must be fixed before item 13**
  (populate real data), since there is no point running scans that
  might silently disappear on the next unattended restart.

  **Options researched (2026-07-06):**

  1. **Persistent disk + existing JSON files** *(recommended)*
     - Render offers persistent disks on any paid tier including the
       current Starter ($7/mo), at **$0.25/GB/month**. A 5 GB disk
       (~30x current projected max size) costs **$1.25/mo**, i.e.
       **~$15/year**.
     - Mount the disk (e.g. at `/var/data`), point `_LEDGER_DIR` at
       the mount path via env var. Zero application-code change to
       ledger logic -- the existing atomic write via tmp-file + rename
       already handles crash safety, and the existing `threading.RLock`
       already handles single-process concurrency (uvicorn is
       single-worker on Render by default).
     - **Implementation effort: ~30 minutes** including redeploy +
       live verification that state survives an instance restart.
     - Trade-offs: disks preclude zero-downtime deploys (few seconds
       of unavailability per redeploy -- acceptable for this internal
       tool) and horizontal scaling (already single-instance -- not
       a change).

  2. **Managed Postgres (Render Postgres, Basic-256mb tier)**
     - Cheapest paid tier: **~$7/mo compute** + $0.30/GB/month
       storage (1 GB baseline included). At current projected 30-county
       size (<200 MB) storage stays under $0.30/mo, so **~$87/year total**.
     - Requires: add `psycopg2` dependency, design schema (coverage_ledger
       table, parcels table with JSONB for geometry_wgs84 + score_breakdown),
       rewrite `_load`/`_save`/`set_zcta_total`/`mark_processed`/
       `save_parcel_results`/`list_all_parcels*` to hit Postgres, handle
       connection pool, add indexes for the property-db list-view queries.
     - **Implementation effort: 4-8 hours** including schema design,
       code migration, testing every endpoint, and one-time data load.
     - Trade-offs: real database (backups, transactions, indexed
       queries, better concurrency headroom) but 6x the cost and
       ~10x the implementation time. Note the free tier (1 GB, expires
       after 30 days) is a dealbreaker -- can't rely on that for
       persistent production data.

  3. **SQLite on persistent disk**
     - Storage cost same as option 1 (~$15/year for the disk).
     - Rewrite ledger to SQLite (stdlib, no new dep). Similar schema
       to Postgres option but no connection pool, no network hop.
     - **Implementation effort: 3-5 hours**.
     - Middle-ground robustness: WAL-mode SQLite handles concurrent
       readers cleanly and does row-level writes instead of full-file
       rewrites. Real query language + indexes. Corruption risk is
       real but SQLite's journaling is well-tested.
     - Not recommended today because option 1 solves the immediate
       problem cheaper and faster; SQLite makes sense as an upgrade
       path later if JSON writes get slow (>100 MB) or query patterns
       need indexes.

  **Recommendation: Option 1 (persistent disk + existing JSON).** Data
  volume is tiny today (~1 MB current, projected <200 MB even after
  item 13 scans 30 counties). Single-process uvicorn writes fit in
  well under a second even at projected max size. Cost is 6x cheaper
  than managed Postgres ($15/yr vs $87/yr). Implementation is one
  order of magnitude faster (30 min vs hours). If load ever grows
  past what JSON handles gracefully, migrating from JSON-on-disk to
  SQLite-on-disk (option 3) later is a straightforward second step;
  starting with Postgres today would be premature scaling for a
  low-write internal tool.

  **Implemented via Option 1 (persistent disk + existing JSON):** Render
  5 GB disk mounted at `/var/data`; `coverage_ledger.py` and
  `background_jobs.py` both read `DATA_DIR` from the environment
  (defaults to `<repo>/data` for local dev so nothing changes offline);
  `/health` surfaces the resolved path so any mount misconfiguration
  is obvious at a glance. Zero application-code change to ledger
  logic itself -- the pre-existing atomic tmp-file + rename write
  pattern and `threading.RLock` already handle single-process crash
  safety.

  **Live-verified end-to-end:** wrote 5 real Pasco parcels via
  `POST /api/coverage/pasco/advance`, snapshotted the parcel IDs,
  triggered a redeploy (commit `811ded0`) -- with a disk attached,
  Render fully stops the old instance before starting the new one,
  so this is a real instance transition, not a graceful reload --
  waited for the new instance to come up, then re-fetched
  `/api/property-db/all`. All 5 parcel IDs survived intact. Compare
  to item 11's evidence where 4 complete Nassau ZCTAs vanished ~15
  min later without even a redeploy.

- [ ] **13. POPULATE REAL DATA -- FULL SCANS ACROSS ALL ACTIVE COUNTIES**
  Once every other roadmap item is complete (including item 8's pipeline
  reordering, item 9's expansion to 30+ confirmed-live counties,
  item 11's ledger-completeness fix, and **item 12's durable
  persistence fix**), run real "Scan entire county" background jobs
  across every confirmed-live county to populate the Property Database
  with real data. **Deliberately sequenced last**: no point generating
  a real dataset before the pipeline computation itself (exclusion
  order, county count, metro-proximity fields) is finalized, the
  completeness signal is trustworthy, and the data has somewhere
  durable to land -- would mean re-scanning every parcel later anyway,
  or worse, running with silently-partial county coverage or watching
  the whole database vanish on the next Render instance restart. This
  is the point where the Property Database home screen actually becomes
  populated with the real, usable candidate list instead of test/mock
  data. **Blocked on items 5, 7, 8, 9, 10, 11, and 12.** **Status:
  not started.** Full detailed instructions will be provided in a
  separate prompt when this item is actively being worked.

## How to use this file

- Mark items complete by changing `- [ ]` to `- [x]` and committing.
- When a new outstanding item is discovered mid-session, add it here
  rather than only in STATUS.md, so it survives into the next session's
  planning.
- STATUS.md continues to be the append-only technical record of what has
  been built and verified. ROADMAP.md is the forward-looking checklist.
