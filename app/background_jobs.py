"""
Background job runner for "Scan entire county" -- daemon thread that
walks the ZIP-sectioned ledger until every ZCTA is complete or the job
is cancelled.

Design (see 2026-07-06 decision write-up):
- One in-flight job per county (idempotent start: a second POST returns
  the existing job's state rather than spawning a duplicate).
- Job state persisted to data/jobs.json at every ledger checkpoint so
  status survives a browser close.
- Cancellation via a flag the worker checks between ZCTAs -- no thread
  hard-kill.
- Render Starter tier assumed (no idle-sleep, so the thread survives
  tab-closed; only a redeploy interrupts it). Interrupted jobs are
  detected at startup and marked so the frontend can offer to resume.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

import coverage_ledger
import scan_orchestrator
import zcta_client


# Where the background-job state JSON lives. Shares the same DATA_DIR
# env-var toggle as the coverage_ledger (roadmap item 12) so both files
# land together on the mounted persistent disk in production. Local dev
# leaves DATA_DIR unset and keeps writing to <repo>/data as before.
_JOBS_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
_JOBS_PATH = os.path.join(_JOBS_DIR, "jobs.json")
_LOCK = threading.RLock()

# In-memory registry of live thread objects. Not persisted; the JSON
# holds the state, this dict lets us cancel a running job.
_LIVE_THREADS: dict[str, threading.Thread] = {}
_CANCEL_FLAGS: dict[str, threading.Event] = {}


@dataclass
class JobState:
    """Everything the frontend needs to render progress + a resume button."""
    county_id: str
    status: str = "queued"  # queued / running / complete / error / cancelled / interrupted
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    current_zcta: Optional[str] = None
    processed_this_run: int = 0
    batches_this_run: int = 0
    error: Optional[str] = None
    # Scan filters/params the job runs with, so the frontend can display
    # them and a resume uses the same settings.
    params: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_all() -> dict[str, dict]:
    if not os.path.exists(_JOBS_PATH):
        return {}
    try:
        with open(_JOBS_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data.get("jobs", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _save_all(jobs: dict[str, dict]) -> None:
    os.makedirs(_JOBS_DIR, exist_ok=True)
    tmp_path = _JOBS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"jobs": jobs}, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _JOBS_PATH)


def _save_state(state: JobState) -> None:
    state.last_updated_at = _now()
    with _LOCK:
        jobs = _load_all()
        jobs[state.county_id] = asdict(state)
        _save_all(jobs)


def get_state(county_id: str) -> Optional[JobState]:
    with _LOCK:
        jobs = _load_all()
        d = jobs.get(county_id)
        if d is None:
            return None
        # Backward-compat: unknown keys are dropped silently; missing
        # keys fall to dataclass defaults.
        allowed = {f.name for f in JobState.__dataclass_fields__.values()}
        d = {k: v for k, v in d.items() if k in allowed}
        return JobState(**d)


def mark_interrupted_at_startup() -> None:
    """
    Called once at app startup: any job that was still 'running' when
    the process previously died gets flipped to 'interrupted' so the
    frontend can offer to resume rather than showing stale progress.
    """
    with _LOCK:
        jobs = _load_all()
        changed = False
        for cid, d in jobs.items():
            if d.get("status") == "running":
                d["status"] = "interrupted"
                d["finished_at"] = _now()
                d["error"] = (
                    "Process restarted while scan was running -- last "
                    "ledger checkpoint is intact, click Resume to continue."
                )
                changed = True
        if changed:
            _save_all(jobs)


def _run_job_loop(county_id: str, params: dict, cancel_flag: threading.Event) -> None:
    """
    The actual worker. Runs in a daemon thread. Advances one ZCTA batch
    at a time, updating state after each batch so the frontend polling
    /job-status sees fresh numbers.
    """
    from county_registry import COUNTIES
    from parcel_fetcher import build_ag_where_clause

    state = get_state(county_id) or JobState(county_id=county_id)
    state.status = "running"
    state.started_at = state.started_at or _now()
    state.error = None
    state.processed_this_run = 0
    state.batches_this_run = 0
    state.params = params
    _save_state(state)

    try:
        county = COUNTIES[county_id]
        zctas = zcta_client.get_county_zctas(county_id)
        zcta_codes = [z["zcta5"] for z in zctas]
        zcta_by_code = {z["zcta5"]: z for z in zctas}

        while True:
            if cancel_flag.is_set():
                state.status = "cancelled"
                state.finished_at = _now()
                _save_state(state)
                return

            next_z = coverage_ledger.next_incomplete_zcta(county_id, zcta_codes)
            if next_z is None:
                state.status = "complete"
                state.finished_at = _now()
                state.current_zcta = None
                _save_state(state)
                return

            state.current_zcta = next_z
            _save_state(state)

            target = zcta_by_code[next_z]

            # Ensure total_candidates is set so completion detection works.
            # Roadmap item 11 (2026-07-06): uses
            # parcel_fetcher.count_matching_candidates so the total matches
            # what the fetcher will actually return -- see main.py's parallel
            # comment for the full context. The old zcta_client.count_parcels_in_zcta
            # was inflating totals by 52% on average across a sampled audit.
            zstate = coverage_ledger.get_zcta_state(county_id, next_z)
            if zstate.get("total_candidates") is None:
                try:
                    from parcel_fetcher import count_matching_candidates
                    total = count_matching_candidates(
                        county_id=county_id,
                        zcta_geometry=target["geometry"],
                        min_acreage=params.get("min_acreage", 20.0),
                        max_acreage=params.get("max_acreage", 4480.0),
                        require_single_owner=params.get("require_single_owner", False),
                    )
                    coverage_ledger.set_zcta_total(county_id, next_z, total)
                except Exception:  # noqa: BLE001 -- count is a cache, retry next batch
                    pass

            already_processed = set(
                coverage_ledger.get_zcta_state(county_id, next_z).get("processed_parcel_ids", [])
            )
            zcta_total = coverage_ledger.get_zcta_state(county_id, next_z).get("total_candidates")

            # If total known and every parcel already processed, mark
            # ZCTA complete explicitly and move on. Guards against the
            # edge case where mark_processed didn't flip .complete due
            # to a total-count mismatch after a partial parse failure.
            if zcta_total is not None and len(already_processed) >= zcta_total:
                # Force complete flag.
                coverage_ledger.set_zcta_total(county_id, next_z, zcta_total)
                continue

            rows = scan_orchestrator.run_county_scan(
                county_id=county_id,
                min_acreage=params.get("min_acreage", 20.0),
                max_acreage=params.get("max_acreage", 4480.0),
                max_candidates=params.get("max_parcels_per_run", 25),
                require_single_owner=params.get("require_single_owner", False),
                min_encirclement_pct=params.get("min_encirclement_pct"),
                flum_character_filter=params.get("flum_character_filter"),
                surrounding_density_filter=params.get("surrounding_density_filter"),
                zcta_geometry=target["geometry"],
                zcta5=next_z,
                skip_parcel_ids=already_processed,
            )

            processed_ids = [r.parcel_id for r in rows if r.parcel_id]
            coverage_ledger.mark_processed(county_id, next_z, processed_ids)
            coverage_ledger.save_parcel_results(
                county_id, scan_orchestrator.rows_to_dicts(rows)
            )

            state.processed_this_run += len(processed_ids)
            state.batches_this_run += 1
            _save_state(state)

            # Empty-batch guard: if the fetcher genuinely returned zero
            # rows AND the ledger still says the ZCTA has remaining
            # parcels, self-heal by re-verifying the total via the same
            # code path as the fetcher (roadmap item 11, 2026-07-06).
            # For ledgers persisted before that fix the stored total was
            # inflated by the OLD zcta_client counter -- if the recomputed
            # total agrees with what's been processed already, the old
            # total was wrong and this ZCTA is actually done. Only surface
            # an error if divergence persists AFTER the self-heal, since
            # that means something genuinely unexpected is happening.
            if not rows and zcta_total and len(already_processed) < zcta_total:
                try:
                    from parcel_fetcher import count_matching_candidates
                    reverified = count_matching_candidates(
                        county_id=county_id,
                        zcta_geometry=target["geometry"],
                        min_acreage=params.get("min_acreage", 20.0),
                        max_acreage=params.get("max_acreage", 4480.0),
                        require_single_owner=params.get("require_single_owner", False),
                    )
                except Exception:  # noqa: BLE001
                    reverified = None
                if reverified is not None and reverified != zcta_total:
                    coverage_ledger.set_zcta_total(county_id, next_z, reverified)
                    # If the healed total now matches processed, treat this
                    # ZCTA as done and continue the job's outer loop.
                    if len(already_processed) >= reverified:
                        continue
                    # Still short after healing -- update loop-local total
                    # so subsequent iterations see the corrected value.
                    zcta_total = reverified
                state.status = "error"
                state.error = (
                    f"Advance for ZCTA {next_z} returned 0 rows but the "
                    f"ZCTA still shows {zcta_total - len(already_processed)} "
                    "candidates remaining after re-verifying against the "
                    "same code path as the fetcher. Likely a real filter/"
                    "layer inconsistency worth investigating rather than a "
                    "stale ledger count."
                )
                state.finished_at = _now()
                _save_state(state)
                return
    except Exception as exc:  # noqa: BLE001 -- surface any error to the UI
        state.status = "error"
        state.error = f"{type(exc).__name__}: {exc}"
        state.finished_at = _now()
        _save_state(state)
    finally:
        _LIVE_THREADS.pop(county_id, None)
        _CANCEL_FLAGS.pop(county_id, None)


def start_full_county_job(county_id: str, params: dict) -> JobState:
    """
    Kick off a background full-county scan. Idempotent: if a job is
    already running for this county, returns its current state instead
    of spawning a duplicate.
    """
    with _LOCK:
        existing = get_state(county_id)
        if existing and existing.status == "running" and county_id in _LIVE_THREADS:
            return existing

        cancel_flag = threading.Event()
        state = JobState(
            county_id=county_id, status="queued",
            started_at=_now(), params=params,
            processed_this_run=0, batches_this_run=0,
        )
        _save_state(state)

        thread = threading.Thread(
            target=_run_job_loop, args=(county_id, params, cancel_flag), daemon=True,
        )
        _LIVE_THREADS[county_id] = thread
        _CANCEL_FLAGS[county_id] = cancel_flag
        thread.start()
        return state


def cancel_job(county_id: str) -> Optional[JobState]:
    with _LOCK:
        flag = _CANCEL_FLAGS.get(county_id)
        if flag:
            flag.set()
        state = get_state(county_id)
        if state and state.status == "running":
            state.status = "cancelled"
            state.finished_at = _now()
            _save_state(state)
        return state
