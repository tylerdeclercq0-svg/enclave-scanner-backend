"""
Coverage ledger -- per-parcel record of which parcels have already had
the full encirclement pipeline run, indexed by county and ZCTA.

Persistence: single JSON file at `data/coverage_ledger.json` relative
to the project root. Simple format, one process, no concurrent writes
expected (single-user analyst tool, one scan at a time).

KNOWN LIMITATION on Render free tier: the container filesystem is
ephemeral -- redeploying (or a container restart) wipes the ledger.
Coverage will need to be re-established after a deploy. Documented in
STATUS.md as an acceptable v1 limitation; a paid disk mount or a
client-side localStorage mirror would fix it, but neither is worth the
complexity for the current use case (one analyst, one desk).

Structure (2026-07-06 pass: also holds the master property database):
{
  "counties": {
    "st_johns": {
      "zctas": {
        "32033": {
          "total_candidates": 577,
          "processed_parcel_ids": ["...", "..."],
          "last_run_at": "2026-07-06T18:32:00Z",
          "complete": false
        }
      },
      "parcels": {
        "140970 0010": {
          <full ScanResultRow as dict, tier, driving_pathways, etc.>,
          "first_scanned_at": "2026-07-06T18:32:00Z",
          "last_scanned_at": "2026-07-06T18:32:00Z"
        }
      }
    }
  }
}

The parcels dict IS the persistent property database Tyler requested:
one entry per parcel_id per county, accumulating across every scan run
over time. Re-scanning a parcel updates its row (and last_scanned_at)
but preserves first_scanned_at. This is the same file/lock/atomic-write
mechanism the ZCTA ledger already uses -- deliberately not a second,
parallel store.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional


# Where the ledger + property database JSON lives on disk. Configurable
# via the DATA_DIR env var so a production deploy can point at a mounted
# persistent disk (Render Starter tier's default filesystem is ephemeral
# and wipes between instance restarts, not just redeploys -- see roadmap
# item 12 for the full context and the ~$15/yr disk math). Local dev
# leaves DATA_DIR unset and keeps writing to <repo>/data as before.
_LEDGER_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
_LEDGER_PATH = os.path.join(_LEDGER_DIR, "coverage_ledger.json")

# 2026-07-12: split the property database out of the ledger. Previously
# every mark_processed and save_parcel_results call did a full load-save
# of coverage_ledger.json -- which held BOTH per-ZCTA progress (small)
# AND per-county parcels dicts (grew unbounded, with heavy
# geometry_wgs84 fields). Peak per-op memory tracked total DB size, not
# operation size; caused a 512 MB Render OOM at ~8 counties scanned.
# Fix: parcels for county X now live in property_db_X.json. Ledger stays
# small; each save touches only one county's file. Legacy parcels still
# in coverage_ledger.json get migrated on first read (one-time cost per
# county).
def _parcels_path(county_id: str) -> str:
    return os.path.join(_LEDGER_DIR, f"property_db_{county_id}.json")

# Simple in-process lock to make concurrent /api/coverage/... calls safe
# even under a threaded uvicorn worker. This isn't multi-process safe
# (uvicorn on Render is single-worker by default), but a threaded server
# accepts overlapping requests and this guard prevents a mid-write read.
_LOCK = threading.RLock()


def _load() -> dict[str, Any]:
    if not os.path.exists(_LEDGER_PATH):
        return {"counties": {}}
    try:
        with open(_LEDGER_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "counties" not in data:
            return {"counties": {}}
        return data
    except (OSError, json.JSONDecodeError):
        # Corrupt file -- start over rather than error out. The ledger is
        # a convenience, not authoritative.
        return {"counties": {}}


def _save(data: dict[str, Any]) -> None:
    os.makedirs(_LEDGER_DIR, exist_ok=True)
    tmp_path = _LEDGER_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _LEDGER_PATH)


def _load_county_parcels(county_id: str) -> dict[str, Any]:
    """
    Read one county's parcels dict. Migrates legacy data on first read:
    if property_db_<county>.json doesn't exist but coverage_ledger.json
    has this county's parcels dict, move them out of the ledger into
    the per-county file (and strip from the ledger). Both writes done
    under _LOCK so a concurrent mark_processed can't race.
    """
    path = _parcels_path(county_id)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    # Migrate from legacy coverage_ledger.json if present.
    legacy = _load()
    legacy_county = legacy.get("counties", {}).get(county_id, {})
    legacy_parcels = legacy_county.get("parcels", {})
    if not legacy_parcels:
        # Nothing to migrate; create empty per-county file so the check
        # above short-circuits next time.
        _save_county_parcels(county_id, {})
        return {}
    # Migrate + strip.
    _save_county_parcels(county_id, dict(legacy_parcels))
    if "parcels" in legacy_county:
        del legacy_county["parcels"]
        _save(legacy)
    return dict(legacy_parcels)


def _save_county_parcels(county_id: str, parcels: dict[str, Any]) -> None:
    os.makedirs(_LEDGER_DIR, exist_ok=True)
    path = _parcels_path(county_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(parcels, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def migrate_legacy_parcels_at_startup() -> dict[str, int]:
    """
    Eagerly move every `parcels` dict out of coverage_ledger.json into
    per-county property_db_<county>.json files. Called from main.py at
    import time, BEFORE any scan thread or request handler runs -- this
    is the lowest-memory-pressure moment in the process lifetime.
    Without this, every subsequent mark_processed call would keep
    reading the full legacy ledger (parcels included) and the OOM would
    repeat. One-time cost per deploy: peaks at the size of the loaded
    legacy dict, then falls back to bounded ZCTA-progress-only memory.
    Returns {county_id: migrated_parcel_count} for logging.
    """
    if not os.path.exists(_LEDGER_PATH):
        return {}
    with _LOCK:
        # This _load is the ONLY point where the full legacy file gets
        # loaded post-fix; every mark_processed after this reads the
        # stripped, small ledger.
        data = _load()
        counties = data.get("counties", {})
        migrated: dict[str, int] = {}
        any_stripped = False
        for cid, cs in counties.items():
            legacy_parcels = cs.get("parcels")
            if not legacy_parcels:
                continue
            # Skip if the per-county file already exists (a previous
            # partial migration or an on-demand _load_county_parcels
            # call may have created it); leave the legacy copy in place
            # rather than overwrite, so no data is silently lost.
            if os.path.exists(_parcels_path(cid)):
                # But still strip the legacy copy since the per-county
                # file is now the source of truth.
                del cs["parcels"]
                any_stripped = True
                continue
            _save_county_parcels(cid, dict(legacy_parcels))
            del cs["parcels"]
            migrated[cid] = len(legacy_parcels)
            any_stripped = True
        if any_stripped:
            _save(data)
        return migrated


def get_county_state(county_id: str) -> dict[str, Any]:
    """Return the raw ledger dict for a county (empty if none), read-only."""
    with _LOCK:
        data = _load()
        return data["counties"].get(county_id, {"zctas": {}})


def get_zcta_state(county_id: str, zcta5: str) -> dict[str, Any]:
    """Return this ZCTA's ledger entry, or a default-empty one."""
    state = get_county_state(county_id)
    return state.get("zctas", {}).get(zcta5, {
        "total_candidates": None,  # not yet enumerated
        "processed_parcel_ids": [],
        "last_run_at": None,
        "complete": False,
    })


def set_zcta_total(county_id: str, zcta5: str, total_candidates: int) -> None:
    """Record how many ag candidates the ZCTA has (used to know when complete)."""
    with _LOCK:
        data = _load()
        cs = data["counties"].setdefault(county_id, {"zctas": {}})
        z = cs["zctas"].setdefault(zcta5, {
            "total_candidates": None,
            "processed_parcel_ids": [],
            "last_run_at": None,
            "complete": False,
        })
        z["total_candidates"] = total_candidates
        # Recompute complete based on the new total.
        z["complete"] = (total_candidates == len(z["processed_parcel_ids"]))
        _save(data)


def mark_processed(county_id: str, zcta5: str, parcel_ids: list[str]) -> None:
    """
    Mark a batch of parcel_ids as fully processed for this ZCTA.
    Idempotent -- re-marking the same parcel_id is a no-op.
    """
    if not parcel_ids:
        return
    with _LOCK:
        data = _load()
        cs = data["counties"].setdefault(county_id, {"zctas": {}})
        z = cs["zctas"].setdefault(zcta5, {
            "total_candidates": None,
            "processed_parcel_ids": [],
            "last_run_at": None,
            "complete": False,
        })
        seen = set(z["processed_parcel_ids"])
        for pid in parcel_ids:
            if pid and pid not in seen:
                z["processed_parcel_ids"].append(pid)
                seen.add(pid)
        z["last_run_at"] = datetime.now(timezone.utc).isoformat()
        if z["total_candidates"] is not None:
            z["complete"] = (len(z["processed_parcel_ids"]) >= z["total_candidates"])
        _save(data)


def is_processed(county_id: str, zcta5: str, parcel_id: str) -> bool:
    return parcel_id in set(get_zcta_state(county_id, zcta5)["processed_parcel_ids"])


def reset_county(county_id: str) -> None:
    """Wipe a county's coverage AND its property DB (start fresh)."""
    with _LOCK:
        data = _load()
        if county_id in data["counties"]:
            del data["counties"][county_id]
            _save(data)
        # Also wipe the per-county property DB file (post-2026-07-12 split).
        path = _parcels_path(county_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def county_summary(county_id: str, all_zctas: list[str]) -> dict[str, Any]:
    """
    Aggregate coverage across every ZCTA the caller says belongs to this
    county. Callers pass the full ZCTA list from zcta_client so the
    ledger doesn't need to duplicate ZCTA enumeration.
    """
    state = get_county_state(county_id)
    zctas_state = state.get("zctas", {})
    complete_count = 0
    total_processed = 0
    total_candidates = 0
    known_total = True
    for z in all_zctas:
        entry = zctas_state.get(z, {})
        processed = len(entry.get("processed_parcel_ids", []))
        total_processed += processed
        if entry.get("total_candidates") is None:
            known_total = False
        else:
            total_candidates += entry["total_candidates"]
        if entry.get("complete"):
            complete_count += 1
    return {
        "zcta_count": len(all_zctas),
        "zctas_complete": complete_count,
        "total_processed_parcels": total_processed,
        "total_candidate_parcels": total_candidates if known_total else None,
        "totals_known": known_total,
        "county_complete": complete_count == len(all_zctas) and len(all_zctas) > 0,
    }


def flag_parcel_no_longer_matching(county_id: str, parcel_id: str, zcta5: str) -> None:
    """
    Mark a parcel that appeared in a previous scan but is NO LONGER in
    the upstream ag-candidate set for its ZCTA at revalidation time
    (roadmap item 19, 2026-07-12). Real diligence signal: the parcel
    was likely sold to a non-ag owner, subdivided, or reclassified out
    of agricultural use. Appends a manual-review note and sets a
    `disappeared_from_upstream_at` ISO timestamp on the row.

    Idempotent: repeat calls update the timestamp but don't stack
    duplicate notes.
    """
    if not parcel_id:
        return
    with _LOCK:
        parcels = _load_county_parcels(county_id)
        row = parcels.get(parcel_id)
        if row is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        note = (
            f"Parcel no longer matches ag-candidate criteria upstream "
            f"in ZCTA {zcta5} as of {now[:10]}. Likely sold, subdivided, "
            f"or reclassified out of agricultural use -- verify current "
            f"status with the county Property Appraiser before pursuing."
        )
        review = list(row.get("needs_manual_review") or [])
        if not any(str(n).startswith("Parcel no longer matches") for n in review):
            review.append(note)
            row["needs_manual_review"] = review
        row["disappeared_from_upstream_at"] = now
        row["last_scanned_at"] = row.get("last_scanned_at") or now
        parcels[parcel_id] = row
        _save_county_parcels(county_id, parcels)


def save_parcel_results(county_id: str, rows: list[dict[str, Any]]) -> None:
    """
    Persist a batch of scan-result rows into the master property
    database. Rows are keyed by parcel_id within their county. Existing
    entries are UPDATED (last_scanned_at bumped, all fields refreshed)
    but first_scanned_at is preserved -- Tyler wants to know when a
    parcel first entered the database, not just when it was last
    touched.

    Since the 2026-07-12 split, each county's parcels live in their own
    property_db_<county>.json file. Only that one file is loaded/saved
    per call -- memory footprint scales with one county's parcel count,
    not the entire cross-county DB.
    """
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        parcels = _load_county_parcels(county_id)
        for row in rows:
            pid = row.get("parcel_id")
            if not pid:
                continue
            existing = parcels.get(pid, {})
            row_copy = dict(row)
            row_copy["first_scanned_at"] = existing.get("first_scanned_at") or now
            row_copy["last_scanned_at"] = now
            parcels[pid] = row_copy
        _save_county_parcels(county_id, parcels)


def list_all_parcels(county_id: str) -> list[dict[str, Any]]:
    """Every parcel ever scanned for this county, unsorted."""
    with _LOCK:
        return list(_load_county_parcels(county_id).values())


def list_all_parcels_all_counties() -> list[dict[str, Any]]:
    """
    Every parcel ever scanned across every county, unsorted. Iterates
    per-county files (post-split) plus anything still in the legacy
    ledger's parcels dicts (for counties not yet migrated). Legacy
    entries are only surfaced when a per-county file doesn't exist
    yet; once _load_county_parcels has migrated a county, its legacy
    parcels are gone from the ledger and only the per-county file
    counts.
    """
    with _LOCK:
        rows: list[dict[str, Any]] = []
        # Enumerate per-county files on disk, PLUS any counties present
        # in the legacy ledger that haven't been migrated yet. Together
        # this is a full sweep.
        try:
            per_county_files = [
                fn for fn in os.listdir(_LEDGER_DIR)
                if fn.startswith("property_db_") and fn.endswith(".json")
            ]
        except FileNotFoundError:
            per_county_files = []
        seen: set[str] = set()
        for fn in per_county_files:
            cid = fn[len("property_db_"):-len(".json")]
            seen.add(cid)
            try:
                with open(os.path.join(_LEDGER_DIR, fn), "r") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    rows.extend(d.values())
            except (OSError, json.JSONDecodeError):
                continue
        # Legacy fallback for anything not yet migrated.
        legacy = _load()
        for cid, cs in legacy.get("counties", {}).items():
            if cid in seen:
                continue
            rows.extend(cs.get("parcels", {}).values())
        return rows


def tier_distribution(county_id: str) -> dict[str, int]:
    """
    Count parcels per tier for a given county. Used for the ranked-view
    header ("st_johns: 4 confirmed, 12 strong, 47 watch, 128 unlikely,
    9 excluded"). Legacy 'confidence_tier' rows (before the master-tier
    field was added) count under their old bucket -- documented, not
    silently reclassified.
    """
    counts: dict[str, int] = {}
    for row in list_all_parcels(county_id):
        t = row.get("tier") or row.get("confidence_tier") or "unlikely"
        counts[t] = counts.get(t, 0) + 1
    return counts


def next_incomplete_zcta(county_id: str, all_zctas: list[str]) -> Optional[str]:
    """
    Return the next ZCTA (ascending by ZCTA5 code -- caller provides the
    sorted list) that isn't yet marked complete. Used by the "advance
    coverage" flow to pick which ZIP section to process next.
    """
    state = get_county_state(county_id)
    zctas_state = state.get("zctas", {})
    for z in all_zctas:
        if not zctas_state.get(z, {}).get("complete"):
            return z
    return None
