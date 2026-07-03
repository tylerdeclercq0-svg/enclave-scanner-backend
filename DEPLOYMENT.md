# Enclave Scanner — Deployment Guide

This is the real, step-by-step path from "code on disk" to "working
website." No step is skippable — each one is a genuine dependency of
the next.

## What you already have

- **Frontend**: `enclave_scanner_prototype.html` — the interactive UI,
  currently using mock data.
- **Backend**: this `enclave-backend/` folder — a FastAPI app wrapping
  the ArcGIS/Census logic, ready to deploy but never run against live
  data (no internet access in the environment that built it).

## Step 1 — Get the backend running somewhere with real internet access

The backend needs a host that keeps a Python process alive. Netlify
(where your frontend lives) does not do this — it's a static file host.

**Recommended: Render.**
1. Go to render.com, sign up (free tier is fine to start).
2. Push this `enclave-backend/` folder to a GitHub repo (or use
   Render's "deploy without git" option if you don't want to set up
   GitHub — but git is much easier for future updates).
3. In Render: New → Web Service → connect the repo.
4. Render should auto-detect `requirements.txt` and `Procfile`. If it
   asks for a build/start command manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Under Environment, add:
   - `CENSUS_API_KEY` — get one free at
     https://api.census.gov/data/key_signup.html (takes a few minutes,
     arrives by email)
   - `FRONTEND_ORIGIN` — your Netlify URL, e.g.
     `https://falconegroupmapping.netlify.app` (you can leave this as
     `*` initially and lock it down once the frontend URL is final)
6. Deploy. Render gives you a URL like
   `https://enclave-scanner-api.onrender.com`.
7. Visit `https://your-render-url.onrender.com/health` in a browser —
   you should see `{"status":"ok"}`. If you see this, the backend is
   alive.

## Step 2 — Confirm at least one county actually works

Before touching the frontend, hit the backend directly:

```
https://your-render-url.onrender.com/api/counties
```

This should return all 7 counties with their live/pending status.

Then try a real scan (start with Hillsborough — the most fully
confirmed endpoint from research):

```
https://your-render-url.onrender.com/api/counties/hillsborough/scan?min_acreage=20&max_acreage=1280
```

**This is the step most likely to surface real problems** — field
names that don't match what research found, ArcGIS services that have
changed since verification, or Shapely geometry edge cases. Expect to
debug here. Common first issues:
- `500: Missing dependency: shapely` — Render didn't install it; check
  `requirements.txt` got picked up.
- Empty `candidates` array — the acreage filter or DOR use code range
  may need adjusting, or the county's cadastral data may use a
  different field structure than expected.
- Timeout — county scans can be slow (paginating through parcels,
  then a spatial query per candidate). Render's free tier has a
  request timeout; if this becomes a problem, the scan needs to move
  to a background job pattern instead of a synchronous request.

## Step 3 — Wire the frontend to the real backend

Right now, `genParcels()` in the HTML file generates mock data. Replace
it with a real fetch call:

```javascript
async function runScan(){
  document.getElementById('nextBtnTop').disabled = true;
  document.getElementById('runScanBtn').disabled = true;
  document.getElementById('runScanBtn').textContent = 'Scanning...';

  const maxAc = document.getElementById('maxAcreage').value;
  const minAc = document.getElementById('minAcreage').value;
  const API_BASE = 'https://your-render-url.onrender.com'; // set this once, at the top of the script

  try {
    const resp = await fetch(`${API_BASE}/api/counties/${selectedCounty}/scan?min_acreage=${minAc}&max_acreage=${maxAc}`);
    if(!resp.ok) throw new Error(`Scan failed: ${resp.status}`);
    const data = await resp.json();
    allParcels = data.candidates; // shape returned by scan_orchestrator.rows_to_dicts()
  } catch (err) {
    alert('Scan failed: ' + err.message);
    document.getElementById('runScanBtn').textContent = 'Run scan';
    document.getElementById('runScanBtn').disabled = false;
    document.getElementById('nextBtnTop').disabled = false;
    return;
  }

  applyFilters();
  document.getElementById('runScanBtn').textContent = 'Run scan';
  document.getElementById('runScanBtn').disabled = false;
  document.getElementById('nextBtnTop').disabled = false;
  goToStep(3);
}
```

**Important field-mapping caveat**: the real backend returns fields
named after `ScanResultRow` in `scan_orchestrator.py`
(`parcel_id`, `pct_perimeter_qualifying`, `likely_pathways`, etc.),
which don't exactly match the mock data's field names
(`id`, `pctEncircled`, `pathways`). The rest of the frontend code
(`confidenceTier()`, `scoreParcel()`, `renderTable()`, etc.) all
reference the mock field names — these need to be updated to match
the real API response shape, or the API response needs a small
translation layer. This is real work, not a one-line fix — budget
real time for it.

Also replace the demographics call similarly, pointing
`pullAreaDemographics()` at
`${API_BASE}/api/parcels/${parcelId}/demographics?lat=...&lon=...`
— note this needs the parcel's actual lat/lon, which the real scan
response should include from its geometry (not present in the mock
data structure at all yet — another real gap to close).

## Step 4 — Deploy the frontend to Netlify

Since you already have a Netlify project:
1. Update the HTML file's `API_BASE` constant to your real Render URL.
2. Drag-and-drop the HTML file into your Netlify project, or connect
   it to a git repo the same way as the backend for easier updates.
3. Netlify gives you a live URL — that's the actual working site.

## What's realistically NOT done yet, even after all four steps

- **Manatee and Sarasota** — their exact field names/layer indices are
  still best-guesses (`LU_DESC`, `FLUNAME`) pending one real
  `describe_layer()` call each. Expect these two to need a fix once
  you hit them with real traffic.
- **Pathways 3 and 4** (interstate + urban service boundary) — hardcoded
  to `False` in `scan_orchestrator.py`. No FDOT roads layer or per-county
  USB layer is wired in yet. Parcels that should qualify via these
  pathways will show as not matching until this is added.
- **Spatial reference mismatches** — Brevard and Volusia's FLUM layers
  use a different coordinate system (Florida State Plane) than the
  statewide cadastral layer. `encirclement.py` doesn't reproject yet —
  this will produce wrong distances if not fixed before relying on
  results for those two counties.
- **Conservation easements** — permanently manual; no data source
  exists. This isn't a bug to fix, just a real limitation.

## Realistic time estimate

If you or a developer are doing this: getting the backend deployed and
`/health` responding is an afternoon. Getting one real county scan
working end-to-end through the frontend, given the field-mapping gap
above, is more like several days of debugging real API responses
against what the code expects. Getting all 7 counties solid is a
couple of weeks of iterative fixing as each county's real data reveals
its own quirks.
