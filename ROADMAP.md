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
  complete. **Status: not started.** Full detailed instructions will
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
