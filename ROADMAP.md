# Roadmap

Prioritized checklist of outstanding work for the FL agricultural enclave
scanner (SB 686 / Ch. 2026-34). This file is the index; `STATUS.md` is the
technical state-of-the-world. Read both at the start of every session.

For every item below, **full detailed instructions will be provided in a
separate prompt when the item is actively being worked** -- this file is
the index/checklist, not the full spec.

## Priority order

- [ ] **1. SECURITY -- lock down `/api/debug/acs-probe`**
  The endpoint at `app/main.py:373` is currently unauthenticated -- it only
  checks that `CENSUS_API_KEY` is set on the server and then proxies
  arbitrary ACS queries out through Render's Census key. Needs to be
  auth-gated (shared secret header or similar) or removed entirely before
  anyone besides Tyler touches this deployment. Quick fix; do this one
  soon. **Status: not started.** Full detailed instructions will be
  provided in a separate prompt when this item is actively being worked.

- [ ] **2. EXPORT REFACTOR -- generalize `exportDiligenceTracker`**
  The function at `web/index.html:2297` is hardcoded to
  `results.filter(r => selectedParcels.has(rowKey(r)))` -- it only reads
  the current-scan `results` array. Needs to be refactored to accept an
  arbitrary parcels array (and its own selection set) so both the
  current-scan export and the future Master DB list-view export call one
  shared function instead of duplicating the payload-building + POST +
  download logic. This is a **blocking dependency for item 4** (the list
  view's checkbox-selection export). **Status: not started.** Full
  detailed instructions will be provided in a separate prompt when this
  item is actively being worked.

- [ ] **3. FOUNDATION REWORK, PART 1 -- Master DB list becomes the primary landing view**
  Restructure so the master database LIST is what the user lands on when
  they open the tool, not the current "Step 1: select county / Step 2: run
  scan" wizard. Scanning controls move to a secondary "Data Collection" /
  "Admin" tab -- they're an ops function, not the primary user flow.
  Depends conceptually on items 4-6 below existing. **Status: not
  started.** Full detailed instructions will be provided in a separate
  prompt when this item is actively being worked.

- [ ] **4. FOUNDATION REWORK, PART 2 -- hide Excluded-tier parcels by default**
  Excluded-tier parcels are currently ranked last in the list, but still
  visible. Change so they're filtered OUT by default, with an explicit
  "Show N excluded" toggle to reveal them -- keeps the primary list a
  workable candidate set, not a review pile. **Blocked on item 2**
  (shared export must be able to respect the visible/hidden filter
  correctly). **Status: not started.** Full detailed instructions will
  be provided in a separate prompt when this item is actively being
  worked.

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

- [ ] **6. FOUNDATION REWORK, PART 4 -- the actual list view**
  Sortable / filterable table view of every parcel ever scanned (same
  data source as the existing Master DB map view at
  `web/index.html:460`). Default sort: tier, then metro-pull score
  (item 5) within tier. Also sortable and groupable by nearest-metro
  name specifically, so the user can pivot into "everything near
  Tampa" / "everything near Orlando." Checkbox selection reusing
  item 2's refactored export function. Layout: Map / List toggle
  inside the existing Master DB overlay (same pattern as Step 3's
  existing List/Map toggle). **Blocked on items 2, 4, and 5.**
  **Status: not started.** Full detailed instructions will be provided
  in a separate prompt when this item is actively being worked.

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

## How to use this file

- Mark items complete by changing `- [ ]` to `- [x]` and committing.
- When a new outstanding item is discovered mid-session, add it here
  rather than only in STATUS.md, so it survives into the next session's
  planning.
- STATUS.md continues to be the append-only technical record of what has
  been built and verified. ROADMAP.md is the forward-looking checklist.
