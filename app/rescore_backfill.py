"""
Per-parcel rescore for the 2026-07-13 built-encirclement fix backfill.

Scope: re-evaluate the master property DB's Option 1 (built-status)
signal for parcels already stored. Motivation: the pre-2026-07-13
pipeline treated any FLUM designation of residential/commercial/
industrial as "qualifying perimeter" for Option 1, producing the false
positives Tyler surfaced (Farmland Reserve / Pioneer HCR / Mathis
Edward Neil, all 100% qualifying on rural land). Since the fix, live
scans compute option1_pct against the county's OWN parcel layer's DOR
use codes; this module retrofits that signal onto rows that predate
the fix.

Design: one-parcel-at-a-time so callers can control cadence, resume on
failure, and see per-parcel progress. The alternative (batch background
job like scan-entire-county) exists elsewhere in this project already;
duplicating that machinery for a one-shot backfill is more infra than
the workload needs.

Scope limits (deliberate, not oversights):
- Only tiers confirmed_qualifying, strong_candidate, watch_list, and
  the new flum_only_verify itself are considered for rescore. `unlikely`
  parcels already had zero pathways match, so a new (fixed) option1_pct
  can only add Option 1 -- which would ONLY fire if built-encirclement
  is >=75%, and a real >=75% built encirclement on a parcel currently
  tiered "unlikely" is exceedingly rare (would mean the FLUM proxy was
  <30% while built proxy is >=75%, a data-source combination not seen
  in the 17k current DB). `excluded` parcels are hard-out regardless.
- Does not re-run the full pipeline (exclusions, FLUM neighbors,
  interstate/USB adjacency, statutory checks). Those don't depend on
  the built-status signal and re-running them multiplies the cost
  ~5x. Only option1_pct / self_surrounding_risk / surrounding_density
  are re-derived; tier + driving_pathways are then re-assigned via
  scoring.assign_master_tier using the stored FLUM/interstate/USB/
  ownership values.
"""

from __future__ import annotations

from typing import Optional

import adjacent_parcels
import coverage_ledger
import scoring
from arcgis_client import query_layer
from county_registry import COUNTIES
from encirclement import determine_pathways, EncirclementResult, PerimeterSegment
from parcel_fetcher import AREA_SR
import service_windows


# Tiers eligible for rescore. Ordered by expected impact -- confirmed_
# qualifying rows are the ones that motivated the fix (the false-
# positive population), strong/watch/flum_only_verify get re-checked
# because a fresh built-status measurement can flip either direction.
RESCORE_TIERS = frozenset({
    "confirmed_qualifying",
    "flum_only_verify",
    "strong_candidate",
    "watch_list",
})


def _fetch_candidate_own_geometry(county, parcel_id: str) -> Optional[dict]:
    """
    Query the county's parcel layer for one specific parcel by id and
    return its geometry (in AREA_SR). Used for the backfill because
    stored geometry_wgs84 rows are in WGS84 (lat/lon) but the
    adjacent-parcel + option1_pct math wants AREA_SR (3086, Florida
    Albers meters). Rather than re-implement WGS84 -> Albers here, one
    query per parcel is the simplest robust path.

    Returns None if the parcel is no longer in the upstream layer (e.g.
    subdivided out, sale-flipped, etc.) -- caller should skip and log.
    """
    if county.parcel_service_url is None or county.parcel_id_field is None:
        return None
    escaped = str(parcel_id).replace("'", "''")
    where = f"{county.parcel_id_field} = '{escaped}'"
    if county.parcel_county_filter:
        where = f"{county.parcel_county_filter} AND {where}"
    features = list(query_layer(
        county.parcel_service_url,
        where=where,
        out_fields=county.parcel_id_field,
        return_geometry=True,
        out_sr=AREA_SR,
    ))
    if not features:
        return None
    return features[0].get("geometry")


def rescore_one_parcel(county_id: str, parcel_id: str) -> dict:
    """
    Fetch the parcel's current geometry + adjacent parcels, re-derive
    the built-status signal, and re-assign the master tier. Writes the
    updated row back to property_db_<county>.json (per the item-12
    per-county-file split).

    Returns a summary dict for the client: old_tier, new_tier,
    option1_pct, adjacent_built_count, self_surrounding_risk,
    changed (bool), and either `error` (if we bailed early) or
    `tier_downgrade_reason` (a plain-English string, populated only
    when the tier changed).

    Raises ValueError only for unknown-county or unknown-parcel cases
    that indicate a caller mistake; everything else is captured on the
    returned dict so a batch client can keep going through partial
    failures.
    """
    county = COUNTIES.get(county_id)
    if county is None:
        raise ValueError(f"Unknown county_id: {county_id}")

    if not service_windows.parcel_source_within_window(county.parcel_source):
        return {
            "county_id": county_id, "parcel_id": parcel_id, "changed": False,
            "error": service_windows.parcel_source_window_message(county.parcel_source),
        }

    parcels = coverage_ledger._load_county_parcels(county_id)  # noqa: SLF001 -- backfill legit reader
    row = parcels.get(parcel_id)
    if row is None:
        raise ValueError(f"Parcel {parcel_id} not found in county {county_id}")

    old_tier = row.get("tier") or row.get("confidence_tier") or "unlikely"

    # Non-eligible tiers: excluded stays excluded (hard statutory hit,
    # not a function of the built-status signal); unlikely rows are
    # skipped per the module docstring's scope-limits note. Caller can
    # still call us for one -- we just report no-op.
    if old_tier not in RESCORE_TIERS:
        return {
            "county_id": county_id, "parcel_id": parcel_id, "changed": False,
            "old_tier": old_tier, "new_tier": old_tier,
            "skipped_reason": f"tier '{old_tier}' outside rescore scope",
        }

    # Fetch the candidate's own current geometry in AREA_SR. This
    # doubles as an "is the parcel still upstream?" check.
    candidate_geom = _fetch_candidate_own_geometry(county, parcel_id)
    if candidate_geom is None:
        return {
            "county_id": county_id, "parcel_id": parcel_id, "changed": False,
            "old_tier": old_tier, "new_tier": old_tier,
            "error": "parcel not found in county parcel layer (may have been "
                     "subdivided or reclassified; weekly reverify would have "
                     "already flagged this via disappeared_from_upstream_at)",
        }

    # Fetch adjacent parcels and derive the new signals.
    try:
        adjacents = adjacent_parcels.fetch_adjacent_parcels(
            county, candidate_geom, parcel_id,
        )
    except RuntimeError as exc:
        return {
            "county_id": county_id, "parcel_id": parcel_id, "changed": False,
            "old_tier": old_tier, "new_tier": old_tier,
            "error": f"adjacent-parcel fetch failed: {exc}",
        }

    option1_raw, adjacent_built_count = adjacent_parcels.compute_option1_pct(
        candidate_geom, adjacents,
    )
    self_surrounding_risk = adjacent_parcels.detect_self_surrounding(
        row.get("owner_name"), adjacents,
    )
    option1_pct = 0.0 if self_surrounding_risk else option1_raw
    surrounding_density = adjacent_parcels.compute_surrounding_density(adjacents)

    # Re-derive likely_pathways with the new option1_pct. Everything
    # else (FLUM proxy, interstate frontage, USB pct, acreage, rural
    # study area = False for all pilot counties, designated pct
    # existing dev = None / structurally unreachable) comes from the
    # stored row, so we don't need to re-fetch FLUM neighbors or roads.
    stored_pct_qualifying = row.get("pct_perimeter_qualifying")
    stored_encirclement = EncirclementResult(
        total_perimeter=1.0,  # only pct_qualifying is read downstream
        qualifying_perimeter=(stored_pct_qualifying or 0.0) / 100.0,
        pct_qualifying=stored_pct_qualifying if stored_pct_qualifying is not None else 0.0,
        segments=[],
        candidate_pathways=[],
    )
    pathways = determine_pathways(
        stored_encirclement,
        acreage=row.get("acreage") or 0.0,
        adjacent_to_interstate=bool(row.get("adjacent_to_interstate")),
        adjacent_to_usb=bool(row.get("adjacent_to_usb")),
        interstate_frontage_pct=row.get("interstate_frontage_pct") or 0.0,
        usb_perimeter_pct=row.get("usb_perimeter_pct") or 0.0,
        option1_pct=option1_pct,
    )

    new_tier, driving = scoring.assign_master_tier(
        exclusion_flags=list(row.get("exclusion_flags") or []),
        likely_pathways=pathways,
        pct_perimeter_qualifying=stored_pct_qualifying,
        interstate_frontage_pct=row.get("interstate_frontage_pct"),
        usb_perimeter_pct=row.get("usb_perimeter_pct"),
        acreage=row.get("acreage"),
        adjacent_to_interstate=bool(row.get("adjacent_to_interstate")),
        adjacent_to_usb=bool(row.get("adjacent_to_usb")),
        single_owner_signal=row.get("single_owner_signal"),
        sold_since_2025=row.get("sold_since_2025"),
        county_has_usb_layer=county.rural_area_layer_url is not None,
        option1_pct=option1_pct,
        self_surrounding_risk=self_surrounding_risk,
    )

    changed = new_tier != old_tier
    tier_downgrade_reason: Optional[str] = None
    if changed:
        # Concise, human-readable "why" for a diligence audit. Kept on
        # the persisted row so a future viewer can see exactly which
        # measurement flipped the tier without re-running anything.
        if new_tier == "flum_only_verify":
            reason_bits = [
                f"FLUM proxy {stored_pct_qualifying:.0f}% qualified perimeter"
                if stored_pct_qualifying is not None else "FLUM proxy unmeasured",
                f"real built-status {option1_pct:.0f}% (needed 75% for Option 1)",
            ]
            if self_surrounding_risk:
                reason_bits.append(
                    "self-surrounding risk (owner on 3+ adjacents; Option 1 capped at 0)"
                )
            tier_downgrade_reason = (
                f"Downgraded from {old_tier} to flum_only_verify on 2026-07-13 "
                f"built-encirclement fix backfill: " + "; ".join(reason_bits)
            )
        else:
            tier_downgrade_reason = (
                f"Retiered from {old_tier} to {new_tier} on 2026-07-13 "
                f"built-encirclement fix backfill; new option1_pct={option1_pct:.0f}%"
            )

    # Update the row in-place. Preserves first_scanned_at, disappeared_
    # from_upstream_at, and every other field not explicitly overwritten
    # here. save_parcel_results would also bump last_scanned_at, but we
    # skip that call: this isn't a fresh scan, it's a re-classification,
    # and bumping last_scanned_at would silently hide when the parcel
    # was really last seen upstream.
    row["option1_pct"] = option1_pct
    row["adjacent_built_count"] = adjacent_built_count
    row["self_surrounding_risk"] = self_surrounding_risk
    row["surrounding_density"] = surrounding_density
    row["likely_pathways"] = pathways
    row["tier"] = new_tier
    row["driving_pathways"] = driving
    if changed:
        row["tier_downgrade_reason"] = tier_downgrade_reason
    parcels[parcel_id] = row
    coverage_ledger._save_county_parcels(county_id, parcels)  # noqa: SLF001

    return {
        "county_id": county_id, "parcel_id": parcel_id,
        "changed": changed,
        "old_tier": old_tier, "new_tier": new_tier,
        "option1_pct": option1_pct,
        "adjacent_built_count": adjacent_built_count,
        "self_surrounding_risk": self_surrounding_risk,
        "surrounding_density": surrounding_density,
        "likely_pathways": pathways,
        "tier_downgrade_reason": tier_downgrade_reason,
    }
