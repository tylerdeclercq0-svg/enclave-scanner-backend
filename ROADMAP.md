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

- [ ] **5. FOUNDATION REWORK, PART 3 -- metro-proximity signal**
  New "metro pull" secondary sort signal. Pull FL Census place-level
  population + median household income (place = incorporated cities +
  Census-designated places, statewide, one-time load). For each scanned
  parcel, find the **nearest place by real great-circle distance** to the
  parcel's centroid -- NOT "whichever result came back first from a
  spatial query," which is the same class of bug already caught and fixed
  once last session for county attribution
  (`find_bg_containing_point` fix, commit `44277a5`). Compute a
  transparent "metro pull" score as a function of nearest-place population,
  nearest-place median income, and distance -- expose all three inputs
  alongside the score so it's auditable, not a black box. **Never**
  blended into the tier or pathway score -- purely a secondary sort key.
  **Status: not started.** Full detailed instructions will be provided in
  a separate prompt when this item is actively being worked.

- [~] **6. FOUNDATION REWORK, PART 4 -- the actual list view** *(core version done 2026-07-06; metro extension deferred to item 5)*
  Core list view lives in the Master DB overlay alongside the existing
  map, with a Map / List toggle at the top and shared `_dbAllParcels`
  data source. Columns: tier, score, driving pathway(s), county,
  parcel_id, acres, owner, ZIP, last scanned. Default sort tier asc +
  score desc, all headers click-to-sort. County + tier filter dropdowns.
  Per-row + select-all checkboxes with independent selection state
  (`_dbSelectedKeys`); "Export selected (.xlsx)" reuses
  `exportDiligenceTracker(parcels)` from item 2 without duplicating
  logic. Column and filter definitions live in `DB_LIST_COLUMNS` /
  `DB_LIST_FILTERS` arrays -- adding metro-proximity columns
  (name / distance / population / income) when item 5 lands is one
  array push per column, not a rewrite. **Remaining under this item:**
  metro-proximity columns + metro-based sort/filter options, blocked on
  item 5. Excluded-tier hiding by default is item 4 (next up), not
  this item.

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

- [ ] **10. VERIFY BACKGROUND SCAN JOB POST-RESTRUCTURING**
  Confirm the "Scan entire county" background-job flow (`background_jobs.py`,
  the polling UI in Data Collection) still works correctly end-to-end after
  this session's Property Database restructuring (two-tab primary nav,
  list-as-home, Data Collection moved behind a tab). Quick regression check,
  not a rebuild -- just confirm the job kicks off from the Data Collection
  tab, the progress polling still updates the UI while the tab isn't
  visible, results flow into the master DB, and the Property Database tab
  reflects the newly-scanned parcels after completion. **Status: not
  started.** Full detailed instructions will be provided in a separate
  prompt when this item is actively being worked.

- [ ] **11. POPULATE REAL DATA -- FULL SCANS ACROSS ALL ACTIVE COUNTIES**
  Once every other roadmap item is complete (including item 8's pipeline
  reordering and item 9's expansion to 30+ confirmed-live counties), run
  real "Scan entire county" background jobs across every confirmed-live
  county to populate the Property Database with real data. **Deliberately
  sequenced last**: no point generating a real dataset before the pipeline
  computation itself (exclusion order, county count, metro-proximity
  fields) is finalized -- would mean re-scanning every parcel later
  anyway. This is the point where the Property Database home screen
  actually becomes populated with the real, usable candidate list instead
  of test/mock data. **Blocked on items 5, 7, 8, 9, and 10.** **Status:
  not started.** Full detailed instructions will be provided in a
  separate prompt when this item is actively being worked.

## How to use this file

- Mark items complete by changing `- [ ]` to `- [x]` and committing.
- When a new outstanding item is discovered mid-session, add it here
  rather than only in STATUS.md, so it survives into the next session's
  planning.
- STATUS.md continues to be the append-only technical record of what has
  been built and verified. ROADMAP.md is the forward-looking checklist.
