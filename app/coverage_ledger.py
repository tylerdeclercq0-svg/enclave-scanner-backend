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

Structure:
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
      }
    }
  }
}
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional


_LEDGER_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_LEDGER_PATH = os.path.join(_LEDGER_DIR, "coverage_ledger.json")

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
    """Wipe a county's coverage (start fresh)."""
    with _LOCK:
        data = _load()
        if county_id in data["counties"]:
            del data["counties"][county_id]
            _save(data)


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
