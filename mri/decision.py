"""
Turning a score into an action.

A number out of 100 is not a decision. Reserve percentage and payout delay are
the levers a merchant of record actually pulls, so those are what this module
returns. The score exists only to select a row in the policy's band table, and
reason codes can override the row the score would have bought.
"""
from __future__ import annotations

from .policy import (
    BAND_SEVERITY,
    POLICY_VERSION,
    REASON_CODES,
    apply_overrides,
    band_by_id,
    band_for_score,
)
from .signals.base import OK


def decide(scoring: dict, codes: list[str], signals: list) -> dict:
    score = scoring.get("score")

    if score is None:
        band = band_by_id("manual_review")
        return {
            "decision": band["decision"],
            "band": band["id"],
            "reserve_pct": band["reserve_pct"],
            "reserve_hold_days": band["reserve_hold_days"],
            "payout": band["payout"],
            "onboarding": band["onboarding"],
            "documents_required": band["documents"],
            "scored_band": None,
            "overrides_applied": [],
            "headline": "Not enough evidence to score",
            "rationale": (
                "Every signal in the policy failed to compute for this domain, so there is no "
                "score to act on. This is an evidence failure, not a merchant failure, and the "
                "application is held rather than declined."
            ),
            "action_items": [
                "Re-run the evaluation; most total failures are transient upstream outages.",
                "If it fails again, confirm the domain resolves and serves a page at all.",
            ],
        }

    scored_band = band_for_score(score)
    final_id, overrides = apply_overrides(scored_band["id"], codes)
    band = band_by_id(final_id)

    return {
        "decision": band["decision"],
        "band": band["id"],
        "reserve_pct": band["reserve_pct"],
        "reserve_hold_days": band["reserve_hold_days"],
        "payout": band["payout"],
        "onboarding": band["onboarding"],
        "documents_required": band["documents"],
        "scored_band": scored_band["id"],
        "overrides_applied": [
            {"code": c, "description": REASON_CODES.get(c, ""),
             "capped_at": _cap_label(c)} for c in overrides
        ],
        "headline": _headline(band, scored_band, overrides),
        "rationale": _rationale(score, scoring, band, scored_band, overrides, signals),
        "action_items": _action_items(band, codes, signals),
        "terms_line": _terms_line(band),
        "policy_version": POLICY_VERSION,
    }


def _cap_label(code: str) -> str:
    from .policy import BAND_OVERRIDES

    return band_by_id(BAND_OVERRIDES.get(code, "decline"))["decision"]


def _terms_line(band: dict) -> str:
    if band["id"] == "auto_approve":
        return "0% reserve, payout T+7."
    if band["id"] == "approve_with_reserve":
        return "5% rolling reserve held 90 days, payout T+14."
    if band["id"] == "manual_review":
        return "Onboarding held. No payout terms until documents clear."
    return "Not boarded."


def _headline(band: dict, scored_band: dict, overrides: list[str]) -> str:
    if overrides and band["id"] != scored_band["id"]:
        return f"{band['decision']} — capped by {overrides[0]}"
    return band["decision"]


def _rationale(score, scoring, band, scored_band, overrides, signals) -> str:
    confidence_pct = round(scoring["confidence"] * 100)
    parts = [
        f"Scored {score:.1f} out of 100 on {scoring['computed_weight']} of 100 policy "
        f"weight points ({confidence_pct}% confidence), which lands in the "
        f"{scored_band['decision'].lower()} band."
    ]

    if overrides and band["id"] != scored_band["id"]:
        code = overrides[0]
        finding = REASON_CODES.get(code, code).rstrip(".")
        parts.append(
            f"{finding}, which caps the outcome at {band['decision'].lower()} regardless of "
            f"score, because that finding is not something a good score elsewhere can "
            f"compensate for."
        )

    weakest = sorted(
        [s for s in signals if s.status == OK and s.normalized is not None],
        key=lambda s: (s.normalized / 100.0) * s.weight - s.weight,
    )[:2]
    if weakest and band["id"] != "auto_approve":
        names = " and ".join(f"{s.label.lower()} ({s.normalized:.0f}/100)" for s in weakest)
        parts.append(f"The heaviest drags on the score were {names}.")

    missing = scoring.get("missing_weight", 0)
    if missing:
        parts.append(
            f"{missing} weight points could not be computed and were removed from the "
            f"denominator rather than scored zero."
        )

    return " ".join(parts)


def _action_items(band: dict, codes: list[str], signals: list) -> list[str]:
    items = []

    if band["id"] == "auto_approve":
        items.append("Board the merchant. No reserve, payouts on T+7.")
        items.append("Standard post-boarding transaction monitoring applies.")
        return items

    if band["id"] == "approve_with_reserve":
        items.append("Board the merchant with a 5% rolling reserve held for 90 days, payouts on T+14.")
        if "NO_REFUND_POLICY" in codes:
            items.append(
                "Require a published refund or cancellation policy before the reserve is released; "
                "its absence is what capped this application."
            )
        if "RECURRING_NO_CANCELLATION" in codes:
            items.append("Require cancellation terms to be published alongside the recurring plans.")
        items.append("Review the reserve at 90 days against realised chargeback rate.")
        return items

    if band["id"] == "manual_review":
        items.append("Hold onboarding and request: certificate of incorporation, "
                     "government photo ID of the beneficial owner, bank account proof in the "
                     "legal entity name.")
        if "CATEGORY_MISMATCH" in codes:
            items.append(
                "Open the site and confirm what it actually sells. The declared category does not "
                "match the content, and that gap is the reason this is not an automated approval."
            )
        if "CATEGORY_RESTRICTED_REVIEW" in codes:
            items.append(
                "Read the storefront and confirm the vertical. Restricted-list keywords appear "
                "in the merchant's own copy but not strongly enough to decline on; this is a "
                "call a person makes, not the keyword scan."
            )
        if "TLS_INVALID" in codes:
            items.append("Require a valid TLS certificate before boarding; card entry is unsafe without one.")
        if "LOW_CONFIDENCE" in codes:
            items.append(
                "Most of the policy could not be computed on this run. Re-run before treating "
                "the score as meaningful."
            )
        return items

    items.append("Decline the application and issue the reason codes below to the merchant.")
    for code in codes:
        if code in ("SAFEBROWSING_HIT", "CATEGORY_RESTRICTED", "PARKED_DOMAIN", "SITE_UNREACHABLE"):
            items.append(f"{code}: {REASON_CODES.get(code, '')}")
    items.append("Retain the stored evaluation record; it is the audit trail if the decision is contested.")
    return items
