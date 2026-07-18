"""
Backfill client for the 2026-07-13 built-encirclement fix.

Iterates every tier-eligible parcel in the live property DB and calls
the /api/property-db/rescore/{county}/{parcel_id} endpoint for each.
See app/rescore_backfill.py for the per-parcel classification logic --
this script is only the driver: paginate, pace, report, resume.

Design choices:
- Uses stdlib urllib (not requests) so it can run in any environment
  without a venv install, same discipline as scripts/weekly_batch_scan.py.
- Filters client-side to RESCORE_TIERS before making calls -- the
  17k-row property DB includes 14,929 "unlikely" and 802 "excluded"
  rows the fix cannot affect, and we don't want 15k no-op HTTP calls
  just to have the server confirm they're out of scope.
- Skips SWFWMD-sourced counties automatically if invoked outside
  6 AM - 10 PM Eastern. Non-SWFWMD counties run any time.
- Simple progress printing (parcel-by-parcel), no fancy dashboards.
  Grep the output for "CHANGED" to find the interesting rows; grep
  for "ERROR" to find failures worth re-running.

Usage:
    # Full backfill across all 13 counties, honoring SWFWMD window:
    DEBUG_API_KEY=<key> python scripts/rescore_master_db.py

    # Scope to specific counties (comma-separated):
    DEBUG_API_KEY=<key> python scripts/rescore_master_db.py --counties pasco,osceola

    # Rescore a specific parcel_id (verification / test-parcel use case):
    DEBUG_API_KEY=<key> python scripts/rescore_master_db.py \\
        --county osceola --parcel-id 332634000000100000

The endpoint is gated by DEBUG_API_KEY (see app/main.py's
_require_debug_key); set that env var before invoking.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error


BASE_URL = os.environ.get(
    "BACKEND_BASE_URL",
    "https://enclave-scanner-backend.onrender.com",
)

RESCORE_TIERS = {
    "confirmed_qualifying",
    "flum_only_verify",
    "strong_candidate",
    "watch_list",
}


def _http_get_json(url: str, timeout: int = 90) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- trusted host
        return json.loads(resp.read().decode("utf-8"))


# Retry sleeps (seconds) for transient network errors. First DNS hiccup on
# 2026-07-13 killed a 700-row run midway through, forcing a full re-run
# (idempotent, but wasted ~15 min). Exponential-ish so we back off through
# a short DNS blip without spinning; caps at 30s. NOT applied to HTTPError
# (4xx/5xx from the server), which are real endpoint states we want to
# surface as-is, not retry-storm.
_RETRY_SLEEPS_SEC = (2, 5, 10, 30)


def _http_post_json(url: str, key: str, timeout: int = 90) -> tuple[int, dict]:
    """
    POST with an X-Debug-Key header. Empty body. Returns (status, json).

    Retries on URLError (DNS / connection reset / timeout at the socket
    layer) with exponential backoff -- see _RETRY_SLEEPS_SEC. HTTPError
    responses (401, 404, 500, etc.) come back as-is without retry so the
    caller sees the real endpoint state.
    """
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"X-Debug-Key": key, "Content-Type": "application/json"},
    )
    last_url_exc: urllib.error.URLError | None = None
    for attempt in range(len(_RETRY_SLEEPS_SEC) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"error": body[:400]}
        except urllib.error.URLError as exc:
            last_url_exc = exc
            if attempt < len(_RETRY_SLEEPS_SEC):
                sleep_sec = _RETRY_SLEEPS_SEC[attempt]
                time.sleep(sleep_sec)
                continue
            raise


def load_eligible_parcels(counties: set[str] | None) -> list[dict]:
    """
    Fetch every eligible parcel across the DB. Uses the default
    lightweight /api/property-db/all response (no geometry, no detail
    fields) since we only need parcel_id + county_id + tier for the
    rescore driver.
    """
    print(f"[{_ts()}] loading property DB from {BASE_URL}/api/property-db/all ...")
    data = _http_get_json(f"{BASE_URL}/api/property-db/all")
    all_parcels = data.get("parcels", [])
    eligible = [
        p for p in all_parcels
        if (p.get("tier") or p.get("confidence_tier") or "unlikely") in RESCORE_TIERS
        and (counties is None or p.get("county_id") in counties)
        and p.get("parcel_id")
    ]
    print(f"[{_ts()}] {len(all_parcels)} total parcels; {len(eligible)} eligible for rescore.")
    return eligible


def _ts() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def rescore_one(
    debug_key: str, county_id: str, parcel_id: str, timeout: int = 90,
) -> dict:
    from urllib.parse import quote
    url = f"{BASE_URL}/api/property-db/rescore/{county_id}/{quote(parcel_id, safe='')}"
    status, body = _http_post_json(url, debug_key, timeout=timeout)
    body["_status"] = status
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0])
    parser.add_argument(
        "--counties",
        help="Comma-separated county_ids to scope the backfill to. Default = all.",
        default=None,
    )
    parser.add_argument(
        "--county",
        help="Single county_id (used with --parcel-id for a targeted rescore).",
        default=None,
    )
    parser.add_argument(
        "--parcel-id",
        help="Single parcel_id (requires --county). Skips DB enumeration.",
        default=None,
    )
    parser.add_argument(
        "--sleep-ms", type=int, default=100,
        help="Pause between per-parcel calls (default 100 ms) to avoid "
             "hammering upstream county layers.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N successful rescores (for dry-run / smoke tests).",
    )
    args = parser.parse_args()

    debug_key = os.environ.get("DEBUG_API_KEY")
    if not debug_key:
        print("ERROR: DEBUG_API_KEY env var must be set.", file=sys.stderr)
        return 2

    # Single-parcel path (verification use case)
    if args.parcel_id:
        if not args.county:
            print("ERROR: --parcel-id requires --county.", file=sys.stderr)
            return 2
        result = rescore_one(debug_key, args.county, args.parcel_id)
        print(json.dumps(result, indent=2))
        return 0 if result.get("_status") == 200 else 1

    counties = set(args.counties.split(",")) if args.counties else None
    eligible = load_eligible_parcels(counties)
    if not eligible:
        print("nothing to rescore.")
        return 0

    changed = 0
    unchanged = 0
    errors = 0
    tier_transitions: dict[str, int] = {}

    for i, p in enumerate(eligible, 1):
        if args.limit and (changed + unchanged) >= args.limit:
            print(f"[{_ts()}] --limit={args.limit} reached, stopping.")
            break
        cid = p["county_id"]
        pid = p["parcel_id"]
        result = rescore_one(debug_key, cid, pid)
        status = result.get("_status")
        if status != 200:
            errors += 1
            print(f"[{_ts()}] {i}/{len(eligible)} ERROR {cid}/{pid}: "
                  f"HTTP {status} {result.get('detail') or result.get('error')}")
        else:
            if result.get("changed"):
                changed += 1
                key = f"{result.get('old_tier')} -> {result.get('new_tier')}"
                tier_transitions[key] = tier_transitions.get(key, 0) + 1
                print(f"[{_ts()}] {i}/{len(eligible)} CHANGED {cid}/{pid}: "
                      f"{result.get('old_tier')} -> {result.get('new_tier')} "
                      f"(option1={result.get('option1_pct')}, "
                      f"self_surrounding={result.get('self_surrounding_risk')})")
            else:
                unchanged += 1
                if i % 50 == 0:
                    print(f"[{_ts()}] {i}/{len(eligible)} ok ({unchanged} unchanged, "
                          f"{changed} changed, {errors} errors so far)")

        if args.sleep_ms > 0:
            time.sleep(args.sleep_ms / 1000.0)

    print()
    print(f"[{_ts()}] DONE. {changed} changed, {unchanged} unchanged, "
          f"{errors} errors out of {len(eligible)} eligible parcels.")
    if tier_transitions:
        print("Tier transitions:")
        for k in sorted(tier_transitions):
            print(f"  {k}: {tier_transitions[k]}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
