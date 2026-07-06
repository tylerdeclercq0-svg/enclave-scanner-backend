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

- [ ] **7. SCALE-UP, PHASE 1 -- statewide reconnaissance across 65 eligible counties**
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

- [ ] **8. SCALE-UP, PHASE 2 -- reorder the per-parcel pipeline for early exclusion gating**
  Move the cheap/fast exclusion checks (cached statewide Wekiva /
  Everglades / ACSC intersects -- effectively free after warm-up per
  Fix A from the 2026-07-06 profiling pass -- and the unincorporated-
  status check) to run as an **early gate** before the expensive
  concurrent FLUM / interstate / USB / water-sewer phase. A parcel that
  gets excluded early should skip everything downstream. **Must be
  profiled before and after against the same 25-parcel Pasco batch used
  for the A/B/C speed fixes** (baseline was 196.88s -> 64.45s after
  Fixes A/B/C -- see STATUS.md's profiling section). This reorder only
  helps parcels that actually get excluded early; measure the real
  impact against real data, don't assume it. **Status: not started.**
  Full detailed instructions will be provided in a separate prompt when
  this item is actively being worked.

- [ ] **9. SCALE-UP, PHASE 3 -- prioritized full verification to >=30 confirmed-live counties**
  Using item 7's triage list, work through counties in priority order to
  reach at least 30 confirmed-live counties. Priority uses the same
  growth-rate / median-income prioritization already used for selecting
  the original four pilot counties (Pasco, Nassau, St. Johns, Osceola).
  Each new county needs the full ground-truthing pass documented in
  STATUS.md: `describe_layer` verification, real field-name confirmation,
  live agricultural-classification test, live end-to-end
  `fetch_candidate_parcels`, and `confirmed_live=True` in
  `county_registry.py`. **Blocked on item 7** (need the triage list to
  pick from). **Status: not started.** Full detailed instructions will
  be provided in a separate prompt when this item is actively being
  worked.

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

- [ ] **11. INVESTIGATE + FIX ZCTA CANDIDATE-COUNT-VS-ACTUAL-QUERY MISMATCH**
  During item 10's Nassau test scan the background job terminated with
  the error *"Advance for ZCTA 31537 returned 0 rows but the ZCTA still
  shows 4 candidates remaining. Filters may be too restrictive; loosen
  them and resume."* This is a real gap in the coverage ledger's
  completeness guarantee -- the ledger's accounting of "remaining
  candidates for this ZCTA" is disagreeing with what the live parcel
  fetch actually returns, so the "county_complete" flag can never
  reliably fire and a runner has no way to distinguish "ZCTA really is
  done" from "ledger says there are 4 more but they're structurally
  unreachable under the current filter." Not just a filter-tuning
  footnote -- needs to be understood (is the ledger overcounting? Are
  the filters silently dropping parcels? Is `mark_processed` failing
  to record some IDs?) and fixed before item 12's real-data population,
  since a bad completeness signal there means half-scanned counties
  silently reported as complete. **Status: not started.** Full detailed
  instructions will be provided in a separate prompt when this item is
  actively being worked.

- [ ] **12. POPULATE REAL DATA -- FULL SCANS ACROSS ALL ACTIVE COUNTIES**
  Once every other roadmap item is complete (including item 8's pipeline
  reordering, item 9's expansion to 30+ confirmed-live counties, and
  item 11's ledger-completeness fix), run real "Scan entire county"
  background jobs across every confirmed-live county to populate the
  Property Database with real data. **Deliberately sequenced last**: no
  point generating a real dataset before the pipeline computation itself
  (exclusion order, county count, metro-proximity fields) is finalized
  and the completeness signal is trustworthy -- would mean re-scanning
  every parcel later anyway, or worse, running with silently-partial
  county coverage. This is the point where the Property Database home
  screen actually becomes populated with the real, usable candidate list
  instead of test/mock data. **Blocked on items 5, 7, 8, 9, 10, and
  11.** **Status: not started.** Full detailed instructions will be
  provided in a separate prompt when this item is actively being
  worked.

## How to use this file

- Mark items complete by changing `- [ ]` to `- [x]` and committing.
- When a new outstanding item is discovered mid-session, add it here
  rather than only in STATUS.md, so it survives into the next session's
  planning.
- STATUS.md continues to be the append-only technical record of what has
  been built and verified. ROADMAP.md is the forward-looking checklist.
