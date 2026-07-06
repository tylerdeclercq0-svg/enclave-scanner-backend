"""
Phase 1 reconnaissance: cheap existence check for a parcel/cadastral
GIS service in every remaining statute-eligible FL county.

Fast/cheap by design -- ArcGIS Online item search + a single ?f=json
verification per top hit. NO field verification, NO describe_layer of
individual layers, NO agricultural-code checking. That's Phase 3.

Output: a triage list per county, one of:
  live    -> a usable parcel service URL was found + responded
  unclear -> a candidate item exists but couldn't be confirmed usable
  none    -> no candidate parcel service found via public search
"""

import concurrent.futures as cf
import json
import re
import sys
import time
import requests

# All 67 FL counties.
ALL_COUNTIES = [
    "Alachua", "Baker", "Bay", "Bradford", "Brevard", "Broward", "Calhoun",
    "Charlotte", "Citrus", "Clay", "Collier", "Columbia", "DeSoto", "Dixie",
    "Duval", "Escambia", "Flagler", "Franklin", "Gadsden", "Gilchrist",
    "Glades", "Gulf", "Hamilton", "Hardee", "Hendry", "Hernando", "Highlands",
    "Hillsborough", "Holmes", "Indian River", "Jackson", "Jefferson",
    "Lafayette", "Lake", "Lee", "Leon", "Levy", "Liberty", "Madison",
    "Manatee", "Marion", "Martin", "Miami-Dade", "Monroe", "Nassau",
    "Okaloosa", "Okeechobee", "Orange", "Osceola", "Palm Beach", "Pasco",
    "Pinellas", "Polk", "Putnam", "St. Johns", "St. Lucie", "Santa Rosa",
    "Sarasota", "Seminole", "Sumter", "Suwannee", "Taylor", "Union",
    "Volusia", "Wakulla", "Walton", "Washington",
]

# Skip: over-cap counties and already-wired (confirmed_live=True in registry).
SKIP_OVER_CAP = {"Miami-Dade", "Broward"}
SKIP_ALREADY_WIRED = {
    "Hillsborough", "Pasco", "Brevard", "Volusia",
    "St. Johns", "Nassau", "Osceola",
}
# Currently in the registry but confirmed_live=False -- include in recon
# so their guessed URLs get an existence check (not a field check).
IN_REGISTRY_UNCONFIRMED = {"Orange", "Sarasota", "Manatee"}


def search_arcgis_online(county: str) -> list[dict]:
    """
    Public item search. Returns candidate items whose title looks like a
    Florida county parcel feature/map service.
    """
    # Two queries per county -- one with "parcels" one with "cadastral" --
    # combined and de-duped downstream. FL constraint via " florida" in
    # title/tags because many counties (e.g. Baker) exist in other states.
    queries = [
        f'title:"{county}" AND title:parcels AND (type:"Feature Service" OR type:"Map Service")',
        f'title:"{county}" AND title:cadastral AND (type:"Feature Service" OR type:"Map Service")',
    ]
    results = []
    for q in queries:
        try:
            resp = requests.get(
                "https://www.arcgis.com/sharing/rest/search",
                params={"q": q, "f": "json", "num": 10, "sortField": "numviews", "sortOrder": "desc"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results", []):
                # Filter to items that mention Florida or FL in some field.
                haystack = " ".join([
                    (item.get("title") or ""),
                    (item.get("snippet") or ""),
                    " ".join(item.get("tags", []) or []),
                    (item.get("owner") or ""),
                    (item.get("url") or ""),
                ]).lower()
                if "florida" in haystack or " fl " in f" {haystack} " or "_fl" in haystack:
                    results.append(item)
        except Exception:
            pass
    # De-dup by id
    seen = set()
    unique = []
    for r in results:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return unique


def verify_service(url: str) -> tuple[bool, str]:
    """
    Cheap existence check: hit the service root with ?f=json and see if
    it responds like an ArcGIS Feature/Map Service (has layers or capabilities).
    Returns (ok, note).
    """
    if not url:
        return False, "empty url"
    try:
        resp = requests.get(url, params={"f": "json"}, timeout=15)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        try:
            data = resp.json()
        except ValueError:
            return False, "non-JSON response"
        if data.get("error"):
            return False, f"service error: {data['error'].get('message', 'unknown')[:80]}"
        # Feature/Map service roots typically have `layers` or `capabilities`
        if "layers" in data or "capabilities" in data or "currentVersion" in data:
            return True, "ok"
        return False, "root response missing layers/capabilities"
    except requests.RequestException as exc:
        return False, f"request failed: {str(exc)[:80]}"


def triage_county(county: str) -> dict:
    items = search_arcgis_online(county)
    if not items:
        return {"county": county, "status": "none", "reason": "no ArcGIS Online items matched FL parcels/cadastral search", "candidate": None}

    # Try the best candidate (highest numviews). If its URL verifies, mark
    # live. If URL exists but doesn't verify, mark unclear.
    for item in items[:3]:  # top 3 candidates
        url = item.get("url")
        if not url:
            continue
        ok, note = verify_service(url)
        if ok:
            return {
                "county": county,
                "status": "live",
                "reason": f"top-candidate service responds ({item.get('title')!r} owner={item.get('owner')})",
                "candidate": {"title": item.get("title"), "owner": item.get("owner"), "url": url, "id": item.get("id"), "numviews": item.get("numviews")},
            }
    # No candidate verified but items exist -- ambiguous.
    top = items[0]
    return {
        "county": county,
        "status": "unclear",
        "reason": f"candidate items found but none responded ({top.get('title')!r} owner={top.get('owner')})",
        "candidate": {"title": top.get("title"), "owner": top.get("owner"), "url": top.get("url"), "id": top.get("id"), "numviews": top.get("numviews")},
    }


def main():
    targets = [c for c in ALL_COUNTIES if c not in SKIP_OVER_CAP and c not in SKIP_ALREADY_WIRED]
    print(f"Recon target set: {len(targets)} counties", file=sys.stderr)
    results = {}
    # Parallel across counties -- ArcGIS Online tolerates modest concurrency.
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(triage_county, c): c for c in targets}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            c = futures[fut]
            try:
                results[c] = fut.result()
            except Exception as exc:
                results[c] = {"county": c, "status": "unclear", "reason": f"triage exception: {str(exc)[:100]}", "candidate": None}
            print(f"  [{i}/{len(targets)}] {c}: {results[c]['status']}", file=sys.stderr)

    # Sort by status then alpha
    order = {"live": 0, "unclear": 1, "none": 2}
    rows = sorted(results.values(), key=lambda r: (order.get(r["status"], 3), r["county"]))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
