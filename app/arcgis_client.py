"""
ArcGIS REST client for the Enclave Scanner.

This module is written to run in an environment with outbound network
access (a real server, a scheduled job, a notebook) — NOT inside this
sandbox, which has no network egress for arbitrary HTTP calls. Treat
this as the implementation to deploy, not something that has been
executed end-to-end here.

Covers two query patterns against ArcGIS Server / ArcGIS Online:
  1. Attribute + pagination queries against the statewide cadastral layer
     (Florida_Statewide_Cadastral), filtered by county FIPS and DOR use
     code range.
  2. Spatial queries against a county's Future Land Use layer, used to
     classify what surrounds a candidate parcel.

ArcGIS REST quirks this code accounts for:
  - maxRecordCount caps how many features a single query can return
    (1,000-2,000 depending on the service) — pagination via resultOffset
    is required for anything larger.
  - Field names are frequently truncated to 10 characters by legacy
    shapefile-derived schemas (e.g. DOR_UC, not DOR_USE_CODE) — always
    confirm exact field names against /query?f=pjson before writing a
    WHERE clause, rather than guessing from a human-readable label.
  - "f=json" must be a literal query parameter, not html — passing f=html
    (the default if omitted) returns a webpage, not data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import requests


DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 1000
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


class ArcGISQueryError(RuntimeError):
    """Raised when an ArcGIS REST query fails after retries, or returns an error payload."""


@dataclass
class QueryResult:
    features: list[dict[str, Any]]
    exceeded_transfer_limit: bool
    fields: list[dict[str, Any]]


def _request_with_retry(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """
    Issue a single ArcGIS REST query with retry on transient failures.
    ArcGIS servers commonly return HTTP 200 with an `{"error": {...}}`
    body rather than a 4xx/5xx status, so both layers of failure are
    checked.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            if "error" in payload:
                raise ArcGISQueryError(
                    f"ArcGIS error on {url}: {payload['error']}"
                )
            return payload
        except (requests.RequestException, ValueError, ArcGISQueryError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
    raise ArcGISQueryError(
        f"Failed to query {url} after {MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def query_layer(
    layer_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    return_geometry: bool = False,
    geometry: Optional[dict[str, Any]] = None,
    geometry_type: str = "esriGeometryPolygon",
    spatial_rel: str = "esriSpatialRelIntersects",
    out_sr: Optional[int] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[dict[str, Any]]:
    """
    Query an ArcGIS feature layer, transparently paging through results.

    Yields one feature dict at a time (each with an "attributes" key and,
    if return_geometry=True, a "geometry" key) so callers can process a
    large county's worth of parcels without holding everything in memory.

    `where` must use the layer's actual field names — confirm these via
    `describe_layer()` below before constructing a WHERE clause, since
    ArcGIS Server commonly truncates or abbreviates field names from
    their human-readable aliases.
    """
    offset = 0
    while True:
        params: dict[str, Any] = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": str(return_geometry).lower(),
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        if geometry is not None:
            params["geometry"] = geometry
            params["geometryType"] = geometry_type
            params["spatialRel"] = spatial_rel
            params["inSR"] = geometry.get("spatialReference", {}).get("wkid", 4326)
        if out_sr is not None:
            params["outSR"] = out_sr

        payload = _request_with_retry(f"{layer_url}/query", params)
        features = payload.get("features", [])
        for feat in features:
            yield feat

        exceeded = payload.get("exceededTransferLimit", False)
        if not exceeded or not features:
            break
        offset += len(features)


def describe_layer(layer_url: str) -> dict[str, Any]:
    """
    Fetch a layer's metadata (field list, geometry type, max record
    count, advanced query support) — run this first against any new
    county endpoint before writing queries against it. This is exactly
    the call that was used during research to confirm each county's
    FLUM service is live and queryable.
    """
    payload = _request_with_retry(layer_url, {"f": "json"})
    return payload


def query_layer_count(layer_url: str, where: str = "1=1") -> int:
    """Cheap existence/sanity check — how many features match a filter, without fetching them."""
    payload = _request_with_retry(
        f"{layer_url}/query",
        {"where": where, "returnCountOnly": "true", "f": "json"},
    )
    return payload.get("count", 0)
