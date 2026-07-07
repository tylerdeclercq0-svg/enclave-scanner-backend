"""
Wave 2 reconnaissance -- try county Property Appraiser direct URLs +
known county GIS URL patterns for the 10 counties Tyler flagged
(Phase 1's "unclear" bucket + the CRITICAL re-check list).

Fast/best-effort. Probes each county's typical Property Appraiser and
GIS URL patterns; for each hit, dumps the layer list + field names for
the layer most likely to be the parcel/cadastral layer.
"""
import sys, os, json
sys.path.insert(0, "app")
import requests

# Wave 2 targets. For each: a list of URL patterns to try, ordered
# most-likely-first.
TARGETS = {
    "Palm Beach": [
        # Palm Beach County GIS (well-known)
        "https://maps.co.palm-beach.fl.us/arcgis/rest/services",
        "https://pbcgis.com/arcgis/rest/services",
        "https://services5.arcgis.com/rBoieBqzAopJgztg/arcgis/rest/services",  # PBC official
    ],
    "Sarasota": [
        "https://ags3.scgov.net/server/rest/services",  # Sarasota County GIS
        "https://gis.sc-pa.com/arcgis/rest/services",  # Property Appraiser
    ],
    "Manatee": [
        "https://services.manateegis.com/arcgis/rest/services",
        "https://gis.manateepao.gov/arcgis/rest/services",  # Property Appraiser
        "https://mcgis.manateecountyfl.gov/arcgis/rest/services",
    ],
    "Orange": [
        "https://maps.orangecountyfl.net/arcgisweb/rest/services",
        "https://ocpaweb.ocpafl.org/arcgis/rest/services",  # Property Appraiser
    ],
    "Seminole": [
        "https://gis.seminolecountyfl.gov/arcgis/rest/services",
        "https://propertyappraiser.seminolecountyfl.gov/arcgis/rest/services",
    ],
    "Alachua": [
        "https://gis.alachuacounty.us/arcgis/rest/services",
        "https://acpafl.org/arcgis/rest/services",  # Property Appraiser
    ],
    "Marion": [
        "https://services.marioncountyfl.org/arcgis/rest/services",
        "https://gis.marioncountyfl.org/arcgis/rest/services",
        "https://www.pa.marion.fl.us/arcgis/rest/services",  # PA
    ],
    "Polk": [
        "https://gis.polk-county.net/arcgis/rest/services",
        "https://gis.polkpa.org/arcgis/rest/services",  # Property Appraiser
    ],
    "Duval": [
        "https://maps.coj.net/publicgis/rest/services",  # City of Jacksonville
        "https://maps.coj.net/agsserv/rest/services",
    ],
    "Monroe": [
        "https://gis.monroecounty-fl.gov/arcgis/rest/services",
        "https://gis.mcpafl.org/arcgis/rest/services",  # Property Appraiser
    ],
}


def probe_root(url, timeout=15):
    """Probe an ArcGIS Server or FeatureServer root. Returns dict of
    findings or None."""
    try:
        r = requests.get(url, params={"f": "json"}, timeout=timeout, verify=False)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        try:
            j = r.json()
        except ValueError:
            return {"error": "non-JSON"}
        return j
    except Exception as exc:
        return {"error": str(exc)[:100]}


def find_parcel_service(root_url):
    """Given an ArcGIS Server /services root, walk one level to find
    services whose name looks parcel-like."""
    j = probe_root(root_url)
    if not j or j.get("error"):
        return None, j.get("error") if j else "no response"
    if "services" not in j:
        # Maybe it's already a service root
        if "layers" in j:
            return root_url, "already a service"
        return None, "not a services root"
    # Look for parcel-flavored services
    candidates = []
    for svc in j.get("services", []):
        name = svc.get("name", "")
        stype = svc.get("type", "")
        if any(kw in name.lower() for kw in ["parcel", "cadastr", "propert"]):
            candidates.append((name, stype))
    # Also list first 5 services as fallback
    return candidates, j.get("services", [])[:8]


# Suppress SSL warnings for these best-effort probes
import warnings
warnings.filterwarnings("ignore")
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

for county, urls in TARGETS.items():
    print(f"\n{'='*70}\n{county}\n{'='*70}")
    for url in urls:
        print(f"  probing: {url}")
        result, extra = find_parcel_service(url) if url.endswith("/services") else (None, probe_root(url))
        if url.endswith("/services"):
            if not result:
                print(f"    -> {extra}")
                continue
            if isinstance(result, str):
                print(f"    -> {result}")
                continue
            if isinstance(result, list) and result:
                print(f"    * parcel-flavored services found:")
                for name, stype in result:
                    print(f"      - {name}  ({stype})")
            else:
                print(f"    -> no parcel-named services (first 8 services: {extra})")
        else:
            j = extra
            if isinstance(j, dict) and j.get("error"):
                print(f"    -> {j['error']}")
            elif isinstance(j, dict) and "layers" in j:
                print(f"    * service root, layers:")
                for lyr in j.get("layers", [])[:8]:
                    print(f"      - {lyr.get('id')}: {lyr.get('name')} ({lyr.get('geometryType', '?')})")
