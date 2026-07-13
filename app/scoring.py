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
    boundary adjacency each contribute half the maximum score.
    `adjacent_to_interstate` is a real, live signal as of 2026-07-06
    (roads_client.py, FDOT's own Interstates layer). `adjacent_to_usb` is
    still hardcoded False upstream (scan_orchestrator.py) — no per-county
    USB layer has been found for any of the four pilot counties (only
    Hillsborough is confirmed to have one; searched live for the other
    four with no result) — treat the USB half of this score as a
    placeholder until that data is connected.
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


# Watch-list bounds -- parcels below the qualifying threshold today but
# potentially close enough that additional surrounding development could
# push them over. Per Tyler 2026-07-06.
WATCH_LIST_MIN_PCT = 30.0
WATCH_LIST_MAX_PCT = 74.0  # up to but excluding the 75% Option 1 threshold


def classify_confidence(
    likely_pathways: list[int],
    exclusion_flags: list[str],
    single_owner_signal: Optional[bool],
    water_sewer_confidence: str,
    pct_perimeter_qualifying: Optional[float] = None,
) -> str:
    """
    Bucket a scanned candidate into "confident" / "possible" / "watch" /
    "unlikely" for the review-candidates UI.

    This is a classification of what's ALREADY been computed elsewhere
    in the pipeline (pathways, exclusions, ownership signal, water/sewer
    estimate, qualifying-perimeter percentage) — no new data source of
    its own. Deliberately conservative: "confident" requires every
    automatable signal to be both present AND favorable, not just "no
    bad news."

    - "unlikely": no pathway matched AND pct_qualifying below the
      watch-list floor (30%). The core legal test fails with today's
      data and the near-perimeter isn't developed enough for a
      surrounding-development change to plausibly flip it.
    - "watch": no pathway matched YET, but pct_qualifying is between
      30% and 74% -- close enough that additional adjacent development
      (or a corrected FLUM designation) could push it over the Option 1
      threshold. Surfaced as a secondary tier so Tyler can revisit
      periodically as surrounding development changes, without mixing
      these in with confirmed qualifiers.
    - "confident": a pathway matched, no real hard-exclusion hit
      (exclusion_flags is empty), the parcel's own record shows no
      co-owner, and the water/sewer estimate has at least "Likely"
      confidence.
    - "possible": a pathway matched but at least one of the above isn't
      confirmed.
    """
    if likely_pathways:
        if (
            not exclusion_flags
            and single_owner_signal is True
            and water_sewer_confidence in ("Known", "Likely")
        ):
            return "confident"
        return "possible"

    # No pathway. Check the watch-list threshold before giving up.
    if pct_perimeter_qualifying is not None and (
        WATCH_LIST_MIN_PCT <= pct_perimeter_qualifying <= WATCH_LIST_MAX_PCT
    ):
        return "watch"
    return "unlikely"


# -------- Master tier ranking (2026-07-06 pass, refined 2026-07-13) --
#
# Turns the per-scan pathway results into a single primary signal per
# parcel -- one of six tiers -- so the ranked property database can be
# sorted by tier first, per-pathway detail second. Deliberately NOT a
# fabricated composite score: each tier's assignment is grounded in
# real pathway percentages against their real statutory thresholds.
#
# Tiers (ordered top-to-bottom in the sort):
#   1. confirmed_qualifying -- >=1 pathway matched AND no unresolved fails
#   2. flum_only_verify     -- FLUM-designation proxy shows >=75%
#                              qualifying perimeter but the REAL built-
#                              status Option 1 test (option1_pct against
#                              actual county parcel use codes, added
#                              2026-07-13) fell short of 75%. These are
#                              the FLUM-only false positives -- they look
#                              qualified on the county's future-land-use
#                              map but the neighboring parcels aren't
#                              actually developed. Need human aerial
#                              review before outreach.
#   3. strong_candidate     -- >=1 pathway matched BUT has fail(s) that
#                              could flip verification (co-owner recorded,
#                              post-2025 sale, etc.)
#   4. watch_list           -- no pathway currently matches, but one or
#                              more pathways show 30-59% real potential
#                              (a plausible future match if surrounding
#                              development continues or a fail resolves)
#   5. unlikely             -- every pathway below the watch-list floor
#   6. excluded             -- hard statutory exclusion hit (Wekiva,
#                              Everglades, ACSC, acreage > 4,480, county
#                              population > 1.75M). Sits alone at the
#                              bottom of the ranked list, never mixed
#                              in with candidates.

# Watch-list per-pathway band. Tighter than the earlier classify_
# confidence band because this is the primary UI signal now, not a
# secondary tier -- 60-74% used to be watch but joins unlikely here
# unless there's a fail that lands the parcel in strong. Per Tyler
# 2026-07-06 spec ("roughly 30-59%").
MASTER_WATCH_MIN_PCT = 30.0
MASTER_WATCH_MAX_PCT = 59.0

# Per-pathway thresholds -- match determine_pathways() in encirclement.py
# so the driving-pathway readouts here are anchored to the real
# statutory tests, not paraphrased.
_PATHWAY_THRESHOLDS = {
    1: 75,  # (c)1.a -- pct_qualifying >= 75
    2: 75,  # (c)1.b -- pct_qualifying >= 75 AND designated_pct >= 75
    3: 75,  # (c)1.c -- (pct_qualifying + interstate_frontage_pct) >= 75
    4: 50,  # (c)2   -- pct_qualifying >= 50 AND usb_perimeter_pct >= 50
    5: 100, # (c)3   -- boolean; if True the parcel is at 100%.
}


def _has_fail_items(
    single_owner_signal: Optional[bool],
    sold_since_2025: Optional[bool],
) -> bool:
    """
    Server-side equivalent of the frontend's buildVerificationChecklist
    "fail" detection. A checklist item ranked 'fail' means the data we
    have shows a real problem -- NOT that the data is missing.

    Per the 2026-07-06 audit (Tyler's "unknown != bad" correction):
    - single_owner_signal is False -> REAL FINDING: county has a co-
      owner field AND this parcel has a co-owner name populated.
      -> genuine fail.
    - single_owner_signal is None -> NO DATA: county's parcel layer has
      no co-owner field at all. NOT a fail. Verify manually via title
      search but do not demote the tier.
    - sold_since_2025 is True -> REAL FINDING: post-1/1/2025 sale
      recorded in the county's own sale-date field(s). -> genuine fail.
    - sold_since_2025 is None -> NO DATA: sale-date encoding not
      configured for this county, or the specific parcel's date fields
      were unparseable. NOT a fail.
    - sold_since_2025 is False -> real finding: last recorded sale was
      pre-2025. Not a fail.
    """
    if single_owner_signal is False:
        return True
    if sold_since_2025 is True:
        return True
    return False


def _driving_pathway_potentials(
    pct_perimeter_qualifying: Optional[float],
    interstate_frontage_pct: Optional[float],
    usb_perimeter_pct: Optional[float],
    acreage: Optional[float],
    adjacent_to_interstate: bool,
    adjacent_to_usb: bool,
    county_has_usb_layer: bool = True,
) -> list[dict]:
    """
    For each currently-implemented pathway, compute a "readiness"
    percentage: the actual live-computed value against that pathway's
    real statutory threshold. Returns a list of per-pathway dicts:
        {option: int, value: float, target: int, at_threshold: bool,
         gated_by: str or None, unmeasurable: bool, label: str}

    `unmeasurable=True` means we can't run this pathway's check for
    reasons that are DATA GAPS, not findings (e.g. St. Johns has no
    rural-area-layer proxy for USB adjacency). Callers use this to
    include the pathway in the driving list with a clear "not
    measurable" label rather than fabricating a zero-percent reading.
    """
    pct_measured = pct_perimeter_qualifying is not None
    pct = pct_perimeter_qualifying if pct_measured else 0.0
    intr = interstate_frontage_pct or 0.0
    usb = usb_perimeter_pct or 0.0
    ac = acreage or 0.0

    out: list[dict] = []

    # Option 1 (c)1.a: raw pct vs 75.
    if pct_measured:
        opt1_label = f"Option 1: {pct:.0f}% qualifying perimeter (needs 75%)"
    else:
        opt1_label = "Option 1: encirclement not measured (needs 75% qualifying perimeter)"
    out.append({
        "option": 1,
        "value": pct,
        "target": _PATHWAY_THRESHOLDS[1],
        "at_threshold": pct_measured and pct >= _PATHWAY_THRESHOLDS[1],
        "gated_by": None,
        "unmeasurable": not pct_measured,
        "label": opt1_label,
    })

    # Option 3 (c)1.c: combined perimeter vs 75, gated on both adjacencies.
    combined = min(100.0, pct + intr)
    gated_3 = None
    # "unmeasurable" only when we ACTUALLY can't rule the pathway out
    # from findings. If the FDOT interstate check ran and returned False,
    # that's a real finding -- Option 3 is ruled out regardless of any
    # USB data gap. Only when interstate adjacency is True (a real
    # finding, meaningful gate) AND USB is a county-wide data gap does
    # the USB gap become the reason Option 3 can't be evaluated.
    option3_unmeasurable = False
    if not pct_measured:
        gated_3 = "encirclement not measured"
        option3_unmeasurable = True
    elif not adjacent_to_interstate:
        # Real FDOT finding -- Option 3 is really out.
        gated_3 = "no interstate adjacency"
    elif not adjacent_to_usb:
        # Interstate side is a real yes, USB side is the reason we can't
        # finish evaluating. If USB layer exists, "no USB adjacency" is a
        # real finding too; if it doesn't, it's a data gap.
        if county_has_usb_layer:
            gated_3 = "no USB adjacency"
        else:
            gated_3 = "USB adjacency not measurable for this county"
            option3_unmeasurable = True
    out.append({
        "option": 3,
        "value": combined if pct_measured else 0.0,
        "target": _PATHWAY_THRESHOLDS[3],
        "at_threshold": combined >= _PATHWAY_THRESHOLDS[3] and gated_3 is None,
        "gated_by": gated_3,
        "unmeasurable": option3_unmeasurable,
        "label": (
            f"Option 3: {combined:.0f}% combined interstate+FLUM perimeter "
            f"(needs 75%{'; ' + gated_3 if gated_3 else ''})"
            if pct_measured
            else f"Option 3: encirclement not measured (needs 75% combined interstate+FLUM)"
        ),
    })

    # Option 4 (c)2: two >=50% tests, gated on acreage <=700.
    gated_4 = None
    if not pct_measured:
        gated_4 = "encirclement not measured"
    elif ac > 700:
        gated_4 = f"{ac:.0f} ac exceeds 700-ac cap"
    limiting = min(pct, usb) if pct_measured else 0.0
    # Same finding-vs-gap logic as Option 3. Option 4 is only truly
    # "unmeasurable" when the FLUM half meets its 50% threshold AND
    # acreage is within the 700-ac cap AND the USB side is a real
    # county-wide data gap. Otherwise a real finding (FLUM half fails,
    # or acreage > 700, or pct not measured) already rules it out.
    option4_unmeasurable = (
        pct_measured
        and pct >= 50
        and ac <= 700
        and not county_has_usb_layer
    )
    if not county_has_usb_layer and option4_unmeasurable:
        # Report the FLUM half honestly, flag the USB half as a data gap.
        opt4_label = (
            f"Option 4: {pct:.0f}% designated-dev perimeter (needs 50%) / "
            f"USB perimeter not measurable for this county"
        )
    elif not pct_measured:
        opt4_label = "Option 4: encirclement not measured (needs 50% + 50% USB)"
        option4_unmeasurable = True
    elif not county_has_usb_layer:
        # FLUM half already fails or acreage gates -> real finding
        opt4_label = (
            f"Option 4: {pct:.0f}% designated-dev perimeter (needs 50%) / "
            f"USB not measurable, but FLUM half already rules Option 4 out"
            f"{'; ' + gated_4 if gated_4 else ''}"
        )
    else:
        opt4_label = (
            f"Option 4: {pct:.0f}% designated-dev perimeter / "
            f"{usb:.0f}% USB perimeter (both need 50%"
            f"{'; ' + gated_4 if gated_4 else ''})"
        )
    out.append({
        "option": 4,
        "value": limiting,
        "target": _PATHWAY_THRESHOLDS[4],
        "at_threshold": (
            pct_measured and pct >= 50 and usb >= 50 and gated_4 is None
            and county_has_usb_layer
        ),
        "gated_by": gated_4,
        "unmeasurable": option4_unmeasurable,
        "label": opt4_label,
    })

    return out


def assign_master_tier(
    *,
    exclusion_flags: list[str],
    likely_pathways: list[int],
    pct_perimeter_qualifying: Optional[float],
    interstate_frontage_pct: Optional[float],
    usb_perimeter_pct: Optional[float],
    acreage: Optional[float],
    adjacent_to_interstate: bool,
    adjacent_to_usb: bool,
    single_owner_signal: Optional[bool],
    sold_since_2025: Optional[bool],
    county_has_usb_layer: bool = True,
    # 2026-07-13 built-encirclement fix. option1_pct is the REAL
    # Option 1 signal against the county's OWN parcel layer's DOR
    # use codes (see adjacent_parcels.py) -- distinct from pct_perimeter_
    # qualifying which is the FLUM-designation proxy. Both are needed
    # here: option1_pct is what determine_pathways uses for the
    # (c)1.a match, and pct_perimeter_qualifying flags the FLUM-only
    # false-positive case (FLUM >=75% + built <75% -> flum_only_verify
    # tier below). None when the adjacent-parcel fetch could not run.
    option1_pct: Optional[float] = None,
    self_surrounding_risk: bool = False,
) -> tuple[str, list[str]]:
    """
    Returns (tier, driving_pathway_labels). Called from scan_orchestrator
    per parcel; the tier is stored on the row AND in the persistent
    coverage_ledger so the master property database can be re-ranked
    without re-running the pipeline.

    driving_pathway_labels is a list of human-readable descriptions of
    which pathway(s) drove this parcel into its tier -- for watch and
    strong especially, that's the whole point ("this is watch because
    Option 4 is at 42% and needs 50%").
    """
    # 1. EXCLUDED dominates everything -- a hard-excluded parcel with
    # even 100% qualifying perimeter is statutorily out, and the ranked
    # list should never let it sit alongside real candidates.
    if exclusion_flags:
        return "excluded", [f"Hard exclusion: {flag[:80]}" for flag in exclusion_flags[:3]]

    potentials = _driving_pathway_potentials(
        pct_perimeter_qualifying=pct_perimeter_qualifying,
        interstate_frontage_pct=interstate_frontage_pct,
        usb_perimeter_pct=usb_perimeter_pct,
        acreage=acreage,
        adjacent_to_interstate=adjacent_to_interstate,
        adjacent_to_usb=adjacent_to_usb,
        county_has_usb_layer=county_has_usb_layer,
    )
    has_fails = _has_fail_items(single_owner_signal, sold_since_2025)

    # 2/3. Pathway matched -> confirmed or strong depending on fails.
    if likely_pathways:
        driving = [p["label"] for p in potentials if p["option"] in likely_pathways]
        if not driving:
            # Shouldn't happen (any matched pathway also has a potentials
            # entry), but guard anyway.
            driving = [f"Option {p} matched" for p in likely_pathways]
        if has_fails:
            fail_notes = []
            if single_owner_signal is False:
                fail_notes.append("co-owner recorded (verify with title search)")
            if sold_since_2025 is True:
                fail_notes.append("post-1/1/2025 sale on record (verify continuity)")
            driving = driving + [f"Unresolved: {n}" for n in fail_notes]
            return "strong_candidate", driving
        return "confirmed_qualifying", driving

    # 3.5 (2026-07-13). FLUM-only-verify: the FLUM-designation proxy
    # says >=75% qualifying perimeter (which would have fired the OLD
    # Option 1 check pre-fix), but the REAL built-status test came
    # back below the 75% threshold. Route these to their own tier so
    # they get human aerial review before outreach rather than either
    # (a) silently sitting in "confirmed_qualifying" (the false-
    # positive path this whole fix exists to close) or (b) silently
    # dropping to "unlikely" (which would lose the FLUM signal
    # entirely -- the underlying county HAS designated the surrounding
    # land for development, that just hasn't happened yet).
    #
    # Self-surrounding-risk parcels (Farmland Reserve / Deseret Ranch
    # etc.) also route here when FLUM >=75%, since the scan
    # orchestrator caps their option1_pct at 0 -- the FLUM designation
    # is still real but the surrounding parcels are all the same
    # owner, so no genuine Option 1 encirclement exists.
    if (
        pct_perimeter_qualifying is not None
        and pct_perimeter_qualifying >= 75
        and (option1_pct is None or option1_pct < 75)
    ):
        driving_notes: list[str] = [
            f"FLUM proxy: {pct_perimeter_qualifying:.0f}% qualifying perimeter "
            f"(pre-2026-07-13 would have fired Option 1)",
        ]
        if option1_pct is not None:
            driving_notes.append(
                f"Real built-status: {option1_pct:.0f}% of perimeter touches "
                f"actually-developed adjacent parcels (needs 75% for genuine "
                f"Option 1)"
            )
        else:
            driving_notes.append(
                "Real built-status: could not be measured -- adjacent-parcel "
                "fetch did not run; verify manually via aerials"
            )
        if self_surrounding_risk:
            driving_notes.append(
                "Self-surrounding risk: candidate's owner appears on 3+ "
                "adjacent parcels; you cannot self-qualify for Option 1"
            )
        return "flum_only_verify", driving_notes

    # 4. Watch-list: no pathway matched, but at least one pathway's
    # readiness value is in the 30-59% band. Report every pathway that
    # qualifies as "driving," not just the highest -- knowing Options 1
    # AND 4 both show potential is more actionable than knowing only the
    # top one.
    # Only pathways where we ACTUALLY MEASURED the readiness count for
    # driving the tier -- an unmeasurable pathway (e.g. Option 4 for a
    # county with no rural-area layer) can't be evidence FOR watch, nor
    # can it be evidence AGAINST it.
    watch_drivers = [
        p["label"] for p in potentials
        if not p.get("unmeasurable")
        and MASTER_WATCH_MIN_PCT <= p["value"] <= MASTER_WATCH_MAX_PCT
    ]
    if watch_drivers:
        # Also mention any pathways we couldn't fully evaluate, so the
        # user knows they weren't ruled out, they just weren't checkable.
        unmeasurable_notes = [
            p["label"] for p in potentials if p.get("unmeasurable")
        ]
        return "watch_list", watch_drivers + [
            f"[data gap] {n}" for n in unmeasurable_notes
        ]

    # 5. Unlikely: nothing meets threshold, nothing measurable in watch
    # band. But if pathway checks were partially unmeasurable, say so --
    # this parcel wasn't demonstrably ruled out on every dimension.
    unmeasurable = [p["label"] for p in potentials if p.get("unmeasurable")]
    if unmeasurable and pct_perimeter_qualifying is None:
        # Encirclement itself couldn't run -- we haven't measured any
        # pathway, so "unlikely" isn't a finding, it's a data gap.
        return "unlikely", [
            "Encirclement measurement missing -- pathway eligibility "
            "could not be evaluated from available data; verify manually.",
        ] + [f"[data gap] {n}" for n in unmeasurable]
    return "unlikely", []
