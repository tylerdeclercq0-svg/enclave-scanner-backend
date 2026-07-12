"""
Weekly cron entrypoint for the auto-populating batch scan (roadmap item 19).

Kicks the batch orchestrator against every confirmed-live county with
`revalidate_before_scan=True`, so each per-county job re-verifies its
complete ZCTAs against upstream before running the normal advance loop.

Run manually:
    python scripts/weekly_batch_scan.py

Run via Render Cron Job:
    Command:   python scripts/weekly_batch_scan.py
    Schedule:  0 12 * * 0   (Sundays 12:00 UTC = 08:00 EDT / 07:00 EST)
    Env vars:  BACKEND_URL (defaults to production Render URL)

Exit code 0 on successful POST, non-zero if the endpoint returned an
error status (Render's cron log will surface it).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error


DEFAULT_URL = "https://enclave-scanner-backend.onrender.com"


def main() -> int:
    base = os.environ.get("BACKEND_URL", DEFAULT_URL).rstrip("/")
    payload = {
        # Empty county_ids -> server defaults to every confirmed_live
        # county in the registry. Currently 13; grows automatically as
        # more counties are wired.
        "county_ids": [],
        "revalidate_before_scan": True,
        # Keep params identical to the manual runs so re-verifications
        # produce comparable results.
        "max_parcels_per_run": 10,
        "min_acreage": 20.0,
        "max_acreage": 4480.0,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/batch/start",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started_at = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            payload_out = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print(f"[weekly_batch_scan] HTTP {exc.code}: {exc.reason}", flush=True)
        try:
            print(exc.read().decode("utf-8", errors="replace"), flush=True)
        except Exception:
            pass
        return 1
    except urllib.error.URLError as exc:
        print(f"[weekly_batch_scan] URL error: {exc.reason}", flush=True)
        return 1

    elapsed = time.time() - started_at
    print(f"[weekly_batch_scan] POST {base}/api/batch/start -> {status} in {elapsed:.2f}s", flush=True)
    print(payload_out, flush=True)
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
