"""
Development attractiveness scoring.

This is explicitly a business-judgment layer, separate from legal
eligibility under SB 686. A parcel can score low here and still be a
valid agricultural enclave, or score high and still fail the statutory
test (e.g. it's encumbered by a conservation easement the score has no
way to see). Keep these two outputs — eligibility flags and
attractiveness score — visually and logically distinct in any UI, never
collapse them into one number.

Weights below (35/25/20/20) match the prototype shown to the user.
These are starting weights, not validated ones — they should be
revisited once real scan results exist and can be checked against
actual deal outcomes.
"""

from __future__ import annotations

from typing import Optional


WEIGHT_ENCIRCLEMENT = 0.35
WEIGHT_ACREAGE_FIT = 0.25
WEIGHT_ACCESS = 0.20
WEIGHT_PATHWAY_REDUNDANCY = 0.20

# Acreage sweet spot: large enough for a meaningful subdivision, small
# enough to stay comfortably under the base 1,280-acre cap. Outside this
# band the score decays linearly rather than dropping off a cliff, since
# a 750-acre parcel is still developable, just less ideal than a
# 300-acre one.
ACREAGE_SWEET_SPOT_MIN = 80
ACREAGE_SWEET_SPOT_MAX = 500
ACREAGE_HARD_CAP = 1280


def _encirclement_score(pct_perimeter_qualifying: Optional[float]) -> float:
    """
    Score scales from 0 at 40% encircled (well below any pathway
    threshold) to 100 at 100% encircled. The statutory minimum for most
    pathways is 50-75%, so a parcel sitting right at 75% scores roughly
    58 — eligible, but with little margin; a parcel at 95% scores 92,
    reflecting that extra margin matters for review risk even though
    the statute only cares about clearing the threshold.
    """
    if pct_perimeter_qualifying is None:
        return 0.0
    return min(100.0, max(0.0, (pct_perimeter_qualifying - 40) / 60 * 100))


def _acreage_fit_score(acreage: Optional[float]) -> float:
    if acreage is None:
        return 0.0
    if ACREAGE_SWEET_SPOT_MIN <= acreage <= ACREAGE_SWEET_SPOT_MAX:
        return 100.0
    if acreage < ACREAGE_SWEET_SPOT_MIN:
        return max(0.0, acreage / ACREAGE_SWEET_SPOT_MIN * 100)
    # Linear decay from 100 at the top of the sweet spot down to 20 at
    # the hard acreage cap.
    span = ACREAGE_HARD_CAP - ACREAGE_SWEET_SPOT_MAX
    over = acreage - ACREAGE_SWEET_SPOT_MAX
    return max(20.0, 100.0 - (over / span * 80))


def _access_score(adjacent_to_interstate: bool, adjacent_to_usb: bool) -> float:
    """
    Simple additive model: interstate adjacency and urban-service-
    boundary adjacency each contribute half the maximum score. Both are
    currently hardcoded to False upstream (scan_orchestrator.py) until
    an FDOT roads layer and per-county USB layers are wired in — treat
    any access score in current output as a placeholder, not a real
    signal, until that data is connected.
    """
    score = 0.0
    score += 50.0 if adjacent_to_interstate else 20.0
    score += 50.0 if adjacent_to_usb else 0.0
    return min(100.0, score)


def _pathway_redundancy_score(pathway_count: int) -> float:
    """
    More independently-qualifying pathways means lower risk that a
    local government's interpretation of one pathway sinks the
    application. Caps at 100 once 3+ pathways apply, since beyond that
    point the marginal risk reduction is small.
    """
    return min(100.0, pathway_count * 40.0)


def score_candidate(
    acreage: Optional[float],
    pct_perimeter_qualifying: Optional[float],
    pathway_count: int,
    adjacent_to_interstate: bool,
    adjacent_to_usb: bool,
) -> tuple[int, dict]:
    """
    Returns (total_score_0_to_100, breakdown_dict). The breakdown is
    intentionally surfaced everywhere this score appears in the UI —
    a single composite number with no visible components invites
    over-trust in a model that is, again, an unvalidated starting
    point.
    """
    encirclement = _encirclement_score(pct_perimeter_qualifying)
    acreage_fit = _acreage_fit_score(acreage)
    access = _access_score(adjacent_to_interstate, adjacent_to_usb)
    redundancy = _pathway_redundancy_score(pathway_count)

    total = (
        encirclement * WEIGHT_ENCIRCLEMENT
        + acreage_fit * WEIGHT_ACREAGE_FIT
        + access * WEIGHT_ACCESS
        + redundancy * WEIGHT_PATHWAY_REDUNDANCY
    )

    breakdown = {
        "encirclement_score": round(encirclement),
        "acreage_fit_score": round(acreage_fit),
        "access_score": round(access),
        "pathway_redundancy_score": round(redundancy),
        "weights": {
            "encirclement": WEIGHT_ENCIRCLEMENT,
            "acreage_fit": WEIGHT_ACREAGE_FIT,
            "access": WEIGHT_ACCESS,
            "pathway_redundancy": WEIGHT_PATHWAY_REDUNDANCY,
        },
    }

    return round(total), breakdown


def classify_confidence(
    likely_pathways: list[int],
    exclusion_flags: list[str],
    single_owner_signal: Optional[bool],
    water_sewer_confidence: str,
) -> str:
    """
    Bucket a scanned candidate into "confident" / "possible" / "unlikely"
    for the review-candidates UI, per Tyler's "Falcone Group v3" mockup.

    This is a classification of what's ALREADY been computed elsewhere
    in the pipeline (pathways, exclusions, ownership signal, water/sewer
    estimate) — it adds no new data source of its own. Deliberately
    conservative: "confident" requires every automatable signal to be
    both present AND favorable, not just "no bad news."

    - "unlikely": no pathway matched at all — the core legal test fails
      with today's data, regardless of anything else.
    - "confident": a pathway matched, no real hard-exclusion hit
      (exclusion_flags is empty — meaningful now that
      exclusions.check_exclusions() only returns genuine hits, not the
      permanent manual-review boilerplate), the parcel's own record
      shows no co-owner, and the water/sewer estimate has at least
      "Likely" confidence (not "Somewhat Likely" or "Unknown").
    - "possible": a pathway matched but at least one of the above isn't
      confirmed — still worth reviewing, just not a slam dunk.
    """
    if not likely_pathways:
        return "unlikely"

    if (
        not exclusion_flags
        and single_owner_signal is True
        and water_sewer_confidence in ("Known", "Likely")
    ):
        return "confident"

    return "possible"
