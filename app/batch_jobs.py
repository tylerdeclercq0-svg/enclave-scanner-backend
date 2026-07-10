"""
Multi-county batch orchestrator (roadmap item 13, 2026-07-09).

Sits on top of `background_jobs.start_full_county_job` to sequence
"Scan entire county" runs across many counties in one shot. Motivation:
running item 13's real-data pass across 13 confirmed-live counties needs
a queue that honors the SWFWMD 6 AM - 10 PM Eastern availability window
for the 6 SWFWMD-sourced counties (Sarasota, Manatee, Hardee, Charlotte,
Marion, Polk) while letting the 7 direct-source counties (Pasco, Nassau,
St. Johns, Osceola, Lee, Leon, Citrus) run any time.

Design:
- One in-flight batch at a time. Second start returns the running one.
- Coordinator daemon thread loops: pick next eligible county, start a
  per-county job via background_jobs, poll it to a terminal state, mark
  it done in batch state, continue.
- Eligibility priority: prefer a pending SWFWMD county when the SWFWMD
  window is open (they're the constrained resource); otherwise take the
  next pending direct-source county. If no county is eligible right now
  (only SWFWMD pending + window closed), park at
  status='paused_awaiting_window' and sleep in-thread until the window
  reopens, same pattern as per-county jobs use.
- Persisted to `batch_state.json` alongside jobs.json / coverage_ledger.
  Startup handler picks up a batch whose process died mid-run and either
  auto-resumes (if the window's open or no window applies) or leaves it
  paused for the next process start.
- Cancellation flags a batch to stop between counties; a per-county job
  already running is also cancelled so the whole thing halts promptly.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import background_jobs
import service_windows


_BATCH_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
_BATCH_PATH = os.path.join(_BATCH_DIR, "batch_state.json")
_LOCK = threading.RLock()

# Single active batch id -- one batch at a time keeps this honest.
_BATCH_ID = "current"

_LIVE_THREAD: Optional[threading.Thread] = None
_CANCEL_FLAG: Optional[threading.Event] = None

# How often the coordinator polls a running per-county job. Kept short
# so cancellation and window transitions land within a couple of ticks.
_POLL_INTERVAL_SEC = 10


@dataclass
class BatchState:
    """State the frontend needs to render batch progress."""
    county_ids: list[str] = field(default_factory=list)
    status: str = "queued"  # queued / running / paused_awaiting_window / complete / cancelled / error / interrupted
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    current_county_id: Optional[str] = None
    completed_county_ids: list[str] = field(default_factory=list)
    errored_county_ids: list[str] = field(default_factory=list)
    resume_at: Optional[str] = None
    error: Optional[str] = None
    params: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_all() -> dict[str, dict]:
    if not os.path.exists(_BATCH_PATH):
        return {}
    try:
        with open(_BATCH_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data.get("batches", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _save_all(batches: dict[str, dict]) -> None:
    os.makedirs(_BATCH_DIR, exist_ok=True)
    tmp_path = _BATCH_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"batches": batches}, f, indent=2, sort_keys=True)
    os.replace(tmp_path, _BATCH_PATH)


def _save_state(state: BatchState) -> None:
    state.last_updated_at = _now()
    with _LOCK:
        batches = _load_all()
        batches[_BATCH_ID] = asdict(state)
        _save_all(batches)


def get_state() -> Optional[BatchState]:
    with _LOCK:
        batches = _load_all()
        d = batches.get(_BATCH_ID)
        if d is None:
            return None
        allowed = {f.name for f in BatchState.__dataclass_fields__.values()}
        d = {k: v for k, v in d.items() if k in allowed}
        return BatchState(**d)


def _pending_counties(state: BatchState) -> list[str]:
    done = set(state.completed_county_ids) | set(state.errored_county_ids)
    return [c for c in state.county_ids if c not in done]


def _pick_next_county(pending: list[str]) -> tuple[Optional[str], Optional[int]]:
    """
    Returns (county_id, wait_seconds_if_none).

    Prefers a SWFWMD-sourced county while the SWFWMD window is open (it's
    the scarce resource). Otherwise takes the next direct-source county.
    If only SWFWMD counties remain and the window is closed, returns
    (None, seconds_until_swfwmd_open).
    """
    from county_registry import COUNTIES
    swfwmd_pending = [c for c in pending if COUNTIES[c].parcel_source == service_windows.SWFWMD_PARCEL_SEARCH]
    direct_pending = [c for c in pending if COUNTIES[c].parcel_source != service_windows.SWFWMD_PARCEL_SEARCH]

    window_open = service_windows.is_within_swfwmd_window()

    if window_open and swfwmd_pending:
        return swfwmd_pending[0], None
    if direct_pending:
        return direct_pending[0], None
    if swfwmd_pending:
        # Only SWFWMD left, window closed -- wait it out.
        return None, service_windows.seconds_until_window_open(service_windows.SWFWMD_PARCEL_SEARCH)
    return None, None  # nothing left


def _poll_until_terminal(county_id: str, cancel_flag: threading.Event) -> str:
    """
    Wait until the per-county job for `county_id` reaches a terminal
    status. Returns the final status string. Respects the batch cancel
    flag by cancelling the per-county job and returning promptly.
    """
    terminal = {"complete", "cancelled", "error", "interrupted"}
    while True:
        if cancel_flag.is_set():
            background_jobs.cancel_job(county_id)
            # Drain to a terminal state so we don't return with a running job.
            js = background_jobs.get_state(county_id)
            return js.status if js else "cancelled"
        js = background_jobs.get_state(county_id)
        if js and js.status in terminal:
            return js.status
        # A per-county job that goes into paused_awaiting_window will
        # auto-resume in its own thread. Just keep polling; the batch
        # doesn't need to intervene.
        threading.Event().wait(_POLL_INTERVAL_SEC)


def _sleep_with_cancel(seconds: int, cancel_flag: threading.Event) -> bool:
    """Sleep in short chunks so cancel lands promptly. True if cancelled."""
    remaining = seconds
    while remaining > 0:
        if cancel_flag.is_set():
            return True
        chunk = min(60, remaining)
        threading.Event().wait(chunk)
        remaining -= chunk
    return False


def _run_batch_loop(cancel_flag: threading.Event) -> None:
    from datetime import timedelta

    state = get_state() or BatchState()
    state.status = "running"
    state.started_at = state.started_at or _now()
    state.error = None
    state.resume_at = None
    _save_state(state)

    try:
        while True:
            if cancel_flag.is_set():
                state.status = "cancelled"
                state.current_county_id = None
                state.finished_at = _now()
                _save_state(state)
                return

            pending = _pending_counties(state)
            if not pending:
                state.status = "complete"
                state.current_county_id = None
                state.finished_at = _now()
                _save_state(state)
                return

            county_id, wait_sec = _pick_next_county(pending)
            if county_id is None:
                # Contract with _pick_next_county:
                #   (None, None) = nothing pending at all -> complete
                #   (None, int)  = wait `int` seconds then re-check
                # Any non-None wait_sec (INCLUDING 0) must trigger the
                # pause/re-check path; the coordinator must not conflate
                # a 0-second wait with "give up." A production abandon-
                # ment on 2026-07-10 traced back to conflating them
                # (the paired int() truncation in service_windows was
                # returning 0 at 05:59:59.9 ET when the window was still
                # closed but <1s away). service_windows now guarantees
                # >=1 when the window is closed, but this branch keeps
                # the semantic split explicit so a future 0 return can't
                # abandon pending work again.
                if wait_sec is not None:
                    resume_dt = datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
                    state.status = "paused_awaiting_window"
                    state.current_county_id = None
                    state.resume_at = resume_dt.isoformat()
                    state.error = service_windows.parcel_source_window_message(service_windows.SWFWMD_PARCEL_SEARCH)
                    _save_state(state)
                    if _sleep_with_cancel(wait_sec, cancel_flag):
                        state.status = "cancelled"
                        state.finished_at = _now()
                        _save_state(state)
                        return
                    state.status = "running"
                    state.resume_at = None
                    state.error = None
                    _save_state(state)
                    continue
                # (None, None) -> nothing left. Genuinely complete.
                state.status = "complete"
                state.current_county_id = None
                state.finished_at = _now()
                _save_state(state)
                return

            state.current_county_id = county_id
            _save_state(state)

            try:
                background_jobs.start_full_county_job(county_id, dict(state.params))
            except Exception as exc:  # window snapped shut between pick and start, etc.
                state.errored_county_ids.append(county_id)
                state.current_county_id = None
                state.error = f"{county_id}: {type(exc).__name__}: {exc}"
                _save_state(state)
                continue

            final_status = _poll_until_terminal(county_id, cancel_flag)
            if cancel_flag.is_set():
                state.status = "cancelled"
                state.current_county_id = None
                state.finished_at = _now()
                _save_state(state)
                return

            if final_status == "complete":
                state.completed_county_ids.append(county_id)
            else:
                # error / cancelled / interrupted at the per-county level
                # -> don't retry within this batch; move on and let the
                # user requeue explicitly.
                state.errored_county_ids.append(county_id)
                js = background_jobs.get_state(county_id)
                state.error = f"{county_id}: {final_status}" + (f" ({js.error})" if js and js.error else "")
            state.current_county_id = None
            _save_state(state)
    except Exception as exc:
        state.status = "error"
        state.error = f"{type(exc).__name__}: {exc}"
        state.finished_at = _now()
        _save_state(state)
    finally:
        global _LIVE_THREAD, _CANCEL_FLAG
        _LIVE_THREAD = None
        _CANCEL_FLAG = None


def start_batch(county_ids: list[str], params: dict) -> BatchState:
    """
    Kick off (or return the already-running) batch. Idempotent: a second
    start with a live coordinator thread returns the current state,
    ignoring the new county_ids/params. To reset, cancel then start.
    """
    global _LIVE_THREAD, _CANCEL_FLAG
    with _LOCK:
        existing = get_state()
        if existing and existing.status in ("running", "paused_awaiting_window") and _LIVE_THREAD is not None:
            return existing

        cancel_flag = threading.Event()
        state = BatchState(
            county_ids=list(county_ids),
            status="queued",
            started_at=_now(),
            params=dict(params),
            completed_county_ids=[],
            errored_county_ids=[],
        )
        _save_state(state)

        thread = threading.Thread(target=_run_batch_loop, args=(cancel_flag,), daemon=True)
        _LIVE_THREAD = thread
        _CANCEL_FLAG = cancel_flag
        thread.start()
        return state


def cancel_batch() -> Optional[BatchState]:
    with _LOCK:
        if _CANCEL_FLAG is not None:
            _CANCEL_FLAG.set()
        state = get_state()
        if state and state.status in ("running", "paused_awaiting_window", "queued"):
            state.status = "cancelled"
            state.finished_at = _now()
            _save_state(state)
        return state


def mark_interrupted_at_startup() -> None:
    """
    Called once at app startup. A batch that was 'running' or
    'paused_awaiting_window' when the process died gets picked back up:
    if the SWFWMD window is open (or the batch has no SWFWMD counties
    left), spawn a fresh coordinator thread with the persisted county
    list + params; otherwise leave it paused for the next restart.
    """
    global _LIVE_THREAD, _CANCEL_FLAG
    with _LOCK:
        state = get_state()
        if state is None:
            return
        if state.status not in ("running", "paused_awaiting_window"):
            return
        pending = _pending_counties(state)
        if not pending:
            state.status = "complete"
            state.finished_at = _now()
            _save_state(state)
            return

        cancel_flag = threading.Event()
        # Reset volatile fields; the coordinator loop will re-pick the
        # next county and set current_county_id itself.
        state.current_county_id = None
        state.resume_at = None
        state.error = None
        state.status = "queued"
        _save_state(state)

        thread = threading.Thread(target=_run_batch_loop, args=(cancel_flag,), daemon=True)
        _LIVE_THREAD = thread
        _CANCEL_FLAG = cancel_flag
        thread.start()
