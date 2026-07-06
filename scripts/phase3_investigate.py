"""
Phase 3 Wave 1 investigation script (roadmap item 9).

For each candidate parcel layer URL from Phase 1's "live" bucket, this
runs describe_layer to dump the actual field schema, plus a small
sample query so we can eyeball whether it's really a per-county parcel
layer (vs. a false-positive like a "vacant parcels overlay" or a
zoning-only layer that doesn't carry use codes).

Purely diagnostic -- no wiring, no county_registry changes.
"""
import sys, os, json
sys.path.insert(0, "app")
import requests
from urllib.parse import urlparse

# Phase 1 recon "live" bucket (10 counties, 9 distinct integration
# efforts since Gadsden/Wakulla share the Leon-hosted layer).
TARGETS = [
    ("Citrus",     "https://services1.arcgis.com/5hzvezV1fsP5byjX/arcgis/rest/services/Citrus_County__FL_Parcels/FeatureServer/11"),
    ("Collier",    "https://services2.arcgis.com/SlIq32SqARUHIhSx/arcgis/rest/services/Parcels/FeatureServer/42"),
    ("Glades",     "https://services6.arcgis.com/90Aakxb3SLGcQGor/arcgis/rest/services/Glades_Parcels2020/FeatureServer/0"),
    ("Hendry",     "https://services7.arcgis.com/8l7Qq5t0CPLAJwJK/arcgis/rest/services/Hendry_County_Parcels/FeatureServer/0"),
    ("Lee",        "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/Lee_County_Parcels/FeatureServer/0"),
    ("Leon",       "https://intervector.leoncountyfl.gov/intervector/rest/services/MapServices/TLC_OverlayParnal_D_WM/MapServer/0"),
    ("Okeechobee", "https://services.arcgis.com/mq0BGE5kHpm8mHFz/arcgis/rest/services/Parcels_Okeechobee/FeatureServer/0"),
    ("Pinellas",   "https://services.arcgis.com/f5HgUpxURgEzTccH/arcgis/rest/services/Pinellas_Parcels_view/FeatureServer/0"),
    ("Gadsden",    "https://cotinter.leoncountyfl.gov/cotinter/rest/services/Vector/COT_OverlayParcels_OtherServiceAreas_D_WM/MapServer/0"),
    ("Wakulla",    "https://cotinter.leoncountyfl.gov/cotinter/rest/services/Vector/COT_OverlayParcels_OtherServiceAreas_D_WM/MapServer/0"),
]


def describe(url):
    try:
        r = requests.get(url, params={"f": "json"}, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"_error": str(exc)[:200]}


def sample(url, count=3):
    """Return count sample features (attributes only)."""
    try:
        r = requests.get(f"{url}/query", params={
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "false",
            "resultRecordCount": count,
            "f": "json",
        }, timeout=45)
        r.raise_for_status()
        j = r.json()
        return j.get("features", [])
    except Exception as exc:
        return [{"_error": str(exc)[:200]}]


for county, url in TARGETS:
    print(f"\n{'='*80}\n{county}   {url}\n{'='*80}")
    d = describe(url)
    if d.get("_error"):
        print(f"describe_layer FAILED: {d['_error']}")
        continue
    print(f"  layer type: {d.get('type')}  name: {d.get('name')!r}  geom: {d.get('geometryType')}")
    fields = d.get("fields", [])
    print(f"  {len(fields)} fields:")
    for f in fields:
        alias = f" (alias={f.get('alias')!r})" if f.get('alias') != f.get('name') else ""
        print(f"    - {f.get('name')} : {f.get('type', '?').replace('esriFieldType', '')}{alias}")
    # Sample 3 rows
    print(f"  --- sample 3 rows ---")
    for feat in sample(url, 3):
        if feat.get("_error"):
            print(f"    QUERY FAILED: {feat['_error']}")
        else:
            attrs = feat.get("attributes", {})
            # Prune to just plausible-signal fields for scanning
            for k, v in attrs.items():
                if v not in (None, "", 0):
                    print(f"    {k}: {v!r}"[:180])
            print("    ---")
