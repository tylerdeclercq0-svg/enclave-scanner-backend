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

import functools
import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

import certifi
import requests


DEFAULT_TIMEOUT = 12
DEFAULT_PAGE_SIZE = 500
MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1

# gis.osceola.org sends only its leaf certificate, no intermediate --
# confirmed via `openssl s_client -showcerts` (1 cert returned, vs. 3 for
# St. Johns' host and 2 for ArcGIS Online). This is a real misconfiguration
# on Osceola's end (curl/Windows schannel masks it by auto-fetching the
# missing intermediate via the cert's AIA extension; Python's requests/
# certifi has no such fallback). Rather than disabling verification, the
# missing intermediate ("Entrust DV TLS Issuing RSA CA 2", fetched from the
# leaf cert's own AIA "CA Issuers" URL) is bundled alongside certifi's
# normal trust store so full chain validation still happens.
_CERTS_DIR = os.path.join(os.path.dirname(__file__), "certs")
_HOST_EXTRA_INTERMEDIATES: dict[str, str] = {
    "gis.osceola.org": os.path.join(_CERTS_DIR, "entrust_dv_tls_issuing_rsa_ca_2.pem"),
}


@functools.lru_cache(maxsize=None)
def _combined_ca_bundle(extra_cert_path: str) -> str:
    """
    Builds (once per process) a temp CA bundle file containing certifi's
    default trust store plus one extra intermediate cert, and returns its
    path for use as `requests`' `verify=` argument.
    """
    with open(certifi.where(), "rb") as f:
        base_bundle = f.read()
    with open(extra_cert_path, "rb") as f:
        extra_cert = f.read()

    fd, path = tempfile.mkstemp(prefix="ag_enclave_ca_bundle_", suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(base_bundle)
        f.write(b"\n")
        f.write(extra_cert)
    return path


def _verify_for_url(url: str) -> "bool | str":
    """Returns the `verify=` value requests should use for a given layer URL."""
    host = urlparse(url).hostname or ""
    extra_cert_path = _HOST_EXTRA_INTERMEDIATES.get(host)
    if extra_cert_path is None:
        return True
    return _combined_ca_bundle(extra_cert_path)


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

    BUG FIX #1: any dict-valued parameter (e.g. `geometry`) must be sent
    as a JSON string, not a raw Python dict — `requests` would
    otherwise serialize it via Python's str()/repr() (single quotes,
    `None` instead of `null`), which ArcGIS's JSON parser rejects with
    a generic "'geometry' parameter is invalid" / "Unexpected
    character encountered" 400 error. Confirmed via live testing.

    BUG FIX #2: uses POST instead of GET. A real county boundary
    polygon (e.g. Hillsborough's actual shape) has thousands of
    coordinate vertices — encoding that as a GET query string produces
    a URL well beyond the ~8KB length most servers/proxies accept,
    resulting in "414 Request-URI Too Long" (confirmed via live
    testing). POST carries the same parameters in the request body,
    which has no comparable length limit, and every ArcGIS REST /query
    endpoint accepts POST identically to GET.
    """
    last_exc: Optional[Exception] = None
    encoded_params = {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in params.items()
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, data=encoded_params, timeout=DEFAULT_TIMEOUT, verify=_verify_for_url(url))
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
    order_by: Optional[str] = None,
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

    `order_by`: optional `orderByFields` value (e.g. "PARNO ASC"). ArcGIS
    documents `resultOffset` pagination stability as guaranteed ONLY when
    the result set has a deterministic ordering -- either an implicit one
    via a layer's `objectIdField`, or an explicit `orderByFields` clause.
    Layers without an objectIdField (confirmed live for SWFWMD's
    parcel_search MapServer) have no server-side default; without an
    explicit order_by, pages CAN silently overlap or skip records under
    server load. Callers that will paginate through a large result set
    should pass a stable, indexed field. Layers WITH an objectIdField
    still work correctly without it, so this parameter is optional.
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
        if order_by is not None:
            params["orderByFields"] = order_by

        payload = _request_with_retry(f"{layer_url}/query", params)
        features = payload.get("features", [])
        for feat in features:
            yield feat

        exceeded = payload.get("exceededTransferLimit", False)
        if not exceeded or not features:
            break
        # BUG FIX #3 (2026-07-10): advance by page_size, NOT len(features).
        # `resultOffset` is a row-index counter into the server's result
        # set, keyed by the ordinal of records that MATCH the query --
        # not by the number of records the server actually returned last
        # page. Some servers (confirmed live against SWFWMD's
        # parcel_search MapServer, which has objectIdField=None and hits
        # a server-side complexity cap when combining spatial +
        # attribute filters) return fewer than resultRecordCount features
        # per page even when exceededTransferLimit=True; advancing by
        # len(features) then lands the next request mid-page-1, causing
        # server-side pagination to re-yield the earlier rows.
        # Concrete measured case: Hardee ZCTA 33834 yielded 972 features
        # (474 unique, 498 duplicates) via the buggy advance and 475
        # features (474 unique, 1 boundary duplicate) via this fix.
        # Direct-source layers that always return full pages
        # (services2.arcgis.com for Nassau, mapping.pascopa.com for
        # Pasco, etc.) were unaffected because len(features) == page_size
        # every iteration -- so this bug went undetected until the first
        # SWFWMD-sourced full-county scan.
        offset += page_size


def describe_layer(layer_url: str) -> dict[str, Any]:
    """
    Fetch a layer's metadata (field list, geometry type, max record
    count, advanced query support) — run this first against any new
    county endpoint before writing queries against it. This is exactly
    the call that was used during research to confirm each county's
    FLUM service is live and queryable.

    Uses GET directly rather than _request_with_retry's POST, since
    this hits the layer's base resource (not /query) and ArcGIS Server
    metadata endpoints reliably support GET but POST support there
    isn't confirmed the way it is for /query.
    """
    resp = requests.get(layer_url, params={"f": "json"}, timeout=DEFAULT_TIMEOUT, verify=_verify_for_url(layer_url))
    resp.raise_for_status()
    return resp.json()


def query_layer_count(layer_url: str, where: str = "1=1") -> int:
    """Cheap existence/sanity check — how many features match a filter, without fetching them."""
    payload = _request_with_retry(
        f"{layer_url}/query",
        {"where": where, "returnCountOnly": "true", "f": "json"},
    )
    return payload.get("count", 0)


def query_layer_ids(
    layer_url: str,
    where: str = "1=1",
    geometry: Optional[dict[str, Any]] = None,
    geometry_type: str = "esriGeometryPolygon",
    spatial_rel: str = "esriSpatialRelIntersects",
) -> list[int]:
    """
    Fetch only the OBJECTIDs matching a filter, without any attributes
    or geometry — the cheapest possible query against a large table.
    Used to size a WHERE clause's real match count before deciding
    whether standard paging or OBJECTID-range batching is appropriate,
    and as the basis for OBJECTID-range batching itself (see
    query_layer_by_id_batches below) to avoid the resultOffset
    performance cliff documented by Esri's own community support forum
    for tables in the multi-million-row range — exactly the situation
    with the Florida statewide cadastral layer (10.8M rows).

    Supports an optional spatial filter (geometry) in addition to the
    attribute where clause — added after live diagnostic testing
    confirmed that filtering the statewide cadastral layer by the
    CO_NO attribute alone times out (CO_NO appears unindexed on that
    layer), while a spatial filter against a boundary polygon is
    expected to use a maintained spatial index instead and behave very
    differently, performance-wise.
    """
    params: dict[str, Any] = {
        "where": where,
        "returnIdsOnly": "true",
        "f": "json",
    }
    if geometry is not None:
        params["geometry"] = geometry
        params["geometryType"] = geometry_type
        params["spatialRel"] = spatial_rel
        params["inSR"] = geometry.get("spatialReference", {}).get("wkid", 4326)
    payload = _request_with_retry(f"{layer_url}/query", params)
    return payload.get("objectIds", []) or []


def query_layer_by_id_batches(
    layer_url: str,
    object_ids: list[int],
    out_fields: str = "*",
    return_geometry: bool = False,
    batch_size: int = 200,
) -> Iterator[dict[str, Any]]:
    """
    Fetch features by explicit OBJECTID batches instead of resultOffset
    paging. This avoids the documented ArcGIS performance cliff where
    deep resultOffset pagination into a multi-million-row table gets
    progressively slower and eventually times out (confirmed via Esri's
    own community support forum — search "resultOffset large feature
    service timeout" for the reference thread). Each batch is a small,
    targeted "OBJECTID IN (...)" query, which the server can typically
    answer via an indexed lookup rather than a table scan.
    """
    for i in range(0, len(object_ids), batch_size):
        batch = object_ids[i:i + batch_size]
        ids_clause = ",".join(str(x) for x in batch)
        where = f"OBJECTID IN ({ids_clause})"
        payload = _request_with_retry(f"{layer_url}/query", {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": str(return_geometry).lower(),
            "f": "json",
        })
        for feat in payload.get("features", []):
            yield feat
