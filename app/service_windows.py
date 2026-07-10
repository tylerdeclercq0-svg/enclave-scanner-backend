"""
Service-availability window enforcement for county data sources that
aren't 24/7 (roadmap Wave 2b follow-up, 2026-07-06).

The SWFWMD parcel_search MapServer -- the discovered shared source for
16 pilot-eligible FL counties (Charlotte, Citrus, DeSoto, Hardee,
Hernando, Highlands, Hillsborough, Lake, Levy, Manatee, Marion, Pasco,
Pinellas, Polk, Sarasota, Sumter) -- documents 6 AM - 10 PM Eastern
daily availability at
www25.swfwmd.state.fl.us/arcgis12/rest/services/BaseVector/parcel_search/MapServer.
Outside that window, requests to the service may fail with 5xx / non-
JSON errors that would confuse the "0 rows but N remaining" ledger
diagnostics (roadmap item 11) or trip a background-job "error" state.

Item 13 will run scans across many counties at once. Silently hitting
this window across all 16 SWFWMD-sourced counties simultaneously would
be a hard-to-diagnose failure. This module enforces the constraint
explicitly:

- Sync /scan and /advance endpoints refuse to run outside the window
  (HTTP 503 with a message naming the next window open time).
- Background /scan-entire-county refuses to START outside the window.
- A background job that's ALREADY running and crosses into the blackout
  window pauses cleanly (status='paused_awaiting_window' with resume_at),
  sleeps in the same thread until the window reopens, then continues
  automatically. On process restart, the startup handler picks up
  paused jobs whose resume_at has passed.

CONCENTRATION-RISK NOTE (see county_registry.py CountyEndpoint.parcel_source
docs and STATUS.md): every county sourced through SWFWMD's shared service
depends on that one third-party mirror. Schema drift or a full outage
there affects every SWFWMD-sourced county simultaneously, not one at a
time. Worth knowing when running item 13.

Add more sources here if other partial-availability endpoints appear.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        _EASTERN = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        # Windows Python doesn't ship IANA tz data unless the `tzdata`
        # PyPI package is installed. `requirements.txt` pins it, so this
        # should only fire in oddball local envs.
        _EASTERN = None
except ImportError:  # very old Python
    _EASTERN = None


# ---- SWFWMD parcel_search window ----------------------------------------

SWFWMD_WINDOW_START_HOUR = 6    # 6 AM Eastern (inclusive)
SWFWMD_WINDOW_END_HOUR = 22     # 10 PM Eastern (exclusive)

# Identifier value written on CountyEndpoint.parcel_source for SWFWMD-
# sourced counties. Used by the enforcement helpers below.
SWFWMD_PARCEL_SEARCH = "swfwmd_parcel_search"


def _to_eastern(now: Optional[datetime]) -> datetime:
    """Normalize input datetime to Eastern (America/New_York) tz-aware."""
    if now is None:
        if _EASTERN is None:
            return datetime.now(timezone.utc)
        return datetime.now(_EASTERN)
    if _EASTERN is None:
        return now
    if now.tzinfo is None:
        # Naive input -- assume Eastern (conservative for local dev).
        return now.replace(tzinfo=_EASTERN)
    return now.astimezone(_EASTERN)


def is_within_swfwmd_window(now: Optional[datetime] = None) -> bool:
    """
    True if it's currently within SWFWMD's 6 AM - 10 PM Eastern window.
    Falls back to True on very old Python without zoneinfo (rather than
    silently blocking everything -- fail open only on the tz layer, not
    the window logic itself).
    """
    if _EASTERN is None:
        return True
    now_e = _to_eastern(now)
    return SWFWMD_WINDOW_START_HOUR <= now_e.hour < SWFWMD_WINDOW_END_HOUR


def next_swfwmd_window_open(now: Optional[datetime] = None) -> datetime:
    """
    The next datetime (Eastern, tz-aware) at which the SWFWMD window
    opens. If we're currently before today's 6 AM, returns today's 6 AM.
    Otherwise returns tomorrow's 6 AM.
    """
    now_e = _to_eastern(now)
    today_open = now_e.replace(hour=SWFWMD_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if now_e < today_open:
        return today_open
    return today_open + timedelta(days=1)


def swfwmd_window_message(now: Optional[datetime] = None) -> str:
    """One-line human-friendly diagnostic for a window rejection."""
    nxt = next_swfwmd_window_open(now)
    return (
        "SWFWMD's shared parcel_search MapServer is only available "
        f"6 AM - 10 PM Eastern. Next window opens at {nxt.isoformat()}."
    )


# ---- Generic per-source enforcement --------------------------------------

def parcel_source_within_window(parcel_source: Optional[str], now: Optional[datetime] = None) -> bool:
    """
    True if a county whose CountyEndpoint.parcel_source is `parcel_source`
    is currently reachable. Returns True for None (24/7 direct-county
    sources like coj.net, mapping.pascopa.com, etc.). Returns True for
    unknown source identifiers so a typo doesn't lock everything up --
    a truly-unavailable service will still surface as a request error.
    """
    if parcel_source is None:
        return True
    if parcel_source == SWFWMD_PARCEL_SEARCH:
        return is_within_swfwmd_window(now)
    return True


def parcel_source_window_message(parcel_source: Optional[str], now: Optional[datetime] = None) -> str:
    """Diagnostic for whichever source is currently blacked out."""
    if parcel_source == SWFWMD_PARCEL_SEARCH:
        return swfwmd_window_message(now)
    return "Data source's availability window is closed."


def seconds_until_window_open(parcel_source: Optional[str], now: Optional[datetime] = None) -> int:
    """
    Seconds until the source's next window open. Returns 0 if the window
    is already open or the source has no window. Used by the background
    worker's sleep-until-resume loop.
    """
    if parcel_source_within_window(parcel_source, now):
        return 0
    now_e = _to_eastern(now)
    if parcel_source == SWFWMD_PARCEL_SEARCH:
        nxt = next_swfwmd_window_open(now_e)
        # math.ceil, not int(). At 05:59:59.9 ET the delta is 0.1s;
        # int() truncates to 0, which a caller can't distinguish from
        # "no wait needed" (the branch above). ceil guarantees >=1s
        # whenever the window is genuinely closed, so a 0 return here
        # always means "already open."
        return max(1, math.ceil((nxt - now_e).total_seconds()))
    return 0
