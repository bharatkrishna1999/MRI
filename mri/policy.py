"""
Versioned underwriting policy.

Everything that can change a decision lives here and is stamped with
POLICY_VERSION. A decision is only reproducible if the policy that produced it
is identifiable, so the version string travels with every stored run, every API
response and every rendered result page.
"""
from __future__ import annotations

# v1.2: absence of a commercial surface stops paying points. Three signals used
# to award partial credit for observing nothing — a site with no billing language
# was scored as "one-time", a site with no price as merely unsized, a site too
# thin to classify as mid-ladder — and on a brochure site those add up to a
# quarter of the score. They now either measure something or abstain. Site
# liveness is reweighted so content depth is half the category, which is what its
# own docstring always claimed. And a site that publishes no policy page, no
# price and no processor raises NO_COMMERCIAL_SURFACE, because those are not
# independent findings to be averaged — they are one finding, observed six times.
# Same 24 signals and same 100 weight points; a v1.1 and a v1.2 decision on the
# same domain can differ, so they do not get to carry the same stamp.
#
# v1.1: category inference reads the pages that describe the offering rather than
# the terms and privacy boilerplate, and a restricted tier has to be confirmed
# before it declines on its own.
POLICY_VERSION = "policy_v1.2"
POLICY_EFFECTIVE = "2026-08-20"

# ── Category weights, 100 points total ──────────────────────────────────────
CATEGORY_WEIGHTS = {
    "domain_identity": 20,
    "site_liveness": 20,
    "commercial_legitimacy": 20,
    "payment_surface": 15,
    "category_risk": 15,
    "reputation": 5,
    "consistency": 5,
}

CATEGORY_LABELS = {
    "domain_identity": "Domain identity",
    "site_liveness": "Site liveness",
    "commercial_legitimacy": "Commercial legitimacy",
    "payment_surface": "Payment surface",
    "category_risk": "Category risk",
    "reputation": "Reputation",
    "consistency": "Consistency",
}

CATEGORY_DESCRIPTIONS = {
    "domain_identity": (
        "What the domain registration record says about how long this merchant has "
        "existed and how much they committed to the name."
    ),
    "site_liveness": (
        "Whether there is a real, serving, non-parked storefront behind the domain "
        "right now."
    ),
    "commercial_legitimacy": (
        "Whether the merchant publishes the policy pages a real seller needs. Refund "
        "policy carries the most weight here because its absence is the strongest "
        "single predictor of chargeback volume."
    ),
    "payment_surface": (
        "What the checkout looks like: who processes payments today, how large the "
        "ticket is, and whether billing recurs."
    ),
    "category_risk": (
        "What the site actually sells, inferred from its own text, scored against the "
        "acceptance taxonomy. Never scored from the merchant's dropdown selection."
    ),
    "reputation": "Third-party threat intelligence on the domain.",
    "consistency": (
        "Whether the merchant's declared details agree with what the infrastructure "
        "and the page footer say."
    ),
}

# ── Signal weights, absolute points out of 100 ──────────────────────────────
# Each entry: (category, points, human label)
SIGNAL_SPEC = {
    # Domain identity — 20
    "domain_age":          ("domain_identity", 8, "Domain age"),
    "registration_term":   ("domain_identity", 4, "Registration term"),
    "privacy_proxy":       ("domain_identity", 4, "Registrant privacy proxy"),
    "tld_abuse":           ("domain_identity", 4, "TLD abuse tier"),
    # Site liveness — 20
    # Content depth is the heaviest of these on purpose, and until v1.2 it was
    # not: 6 points against 14 for the four cheap ones. Serving HTTP 200 with a
    # valid certificate and a fast first byte is free — a $12 domain behind a CDN,
    # ten minutes of work — while publishing several hundred words about a real
    # product is not. At 6/20 the cheap signals outvoted the expensive one better
    # than two to one, which is how a 125-word brochure scored 76/100 for having
    # a "real, serving storefront". Content depth is now 40% of its category,
    # exactly as refund policy is 40% of commercial legitimacy, and for the same
    # reason: it is the one member of the category that costs the merchant
    # something to satisfy. It stops there rather than going higher, because
    # refund policy is the heaviest single signal in this policy and a proxy for
    # effort should not outrank a direct predictor of chargebacks.
    "http_root":           ("site_liveness", 3, "HTTP root response"),
    "tls":                 ("site_liveness", 3, "TLS certificate"),
    "content_depth":       ("site_liveness", 8, "Content depth"),
    "parked":              ("site_liveness", 4, "Parked page check"),
    "ttfb":                ("site_liveness", 2, "Time to first byte"),
    # Commercial legitimacy — 20 (refund is 40% of the category by design)
    "refund_policy":       ("commercial_legitimacy", 8, "Refund or cancellation policy"),
    "terms_page":          ("commercial_legitimacy", 3, "Terms page"),
    "privacy_page":        ("commercial_legitimacy", 3, "Privacy page"),
    "contact_page":        ("commercial_legitimacy", 3, "Contact page"),
    "pricing_page":        ("commercial_legitimacy", 3, "Pricing page"),
    # Payment surface — 15
    "processor":           ("payment_surface", 7, "Incumbent processor detected"),
    "price_point":         ("payment_surface", 4, "Highest listed price point"),
    "recurring_billing":   ("payment_surface", 4, "Recurring billing"),
    # Category risk — 15
    "category_tier":       ("category_risk", 9, "Inferred category tier"),
    "category_mismatch":   ("category_risk", 3, "Declared vs inferred category"),
    "restricted_keywords": ("category_risk", 3, "Restricted keyword scan"),
    # Reputation — 5
    "safe_browsing":       ("reputation", 5, "Google Safe Browsing"),
    # Consistency — 5
    "geo_consistency":     ("consistency", 2, "Country vs GeoIP vs ccTLD"),
    "legal_name_match":    ("consistency", 2, "Legal name vs site footer"),
    "email_domain_match":  ("consistency", 1, "Email domain vs site domain"),
}

TOTAL_WEIGHT = sum(points for _, points, _ in SIGNAL_SPEC.values())
assert TOTAL_WEIGHT == 100, f"policy weights must total 100, got {TOTAL_WEIGHT}"

for _cat, _pts in CATEGORY_WEIGHTS.items():
    _sum = sum(p for c, p, _ in SIGNAL_SPEC.values() if c == _cat)
    assert _sum == _pts, f"{_cat} signals total {_sum}, category weight is {_pts}"


# ── Decision bands ──────────────────────────────────────────────────────────
# Reserve percentage and payout delay are the levers a merchant of record
# actually pulls. The score only exists to select one of these rows.
DECISION_BANDS = [
    {
        "id": "auto_approve",
        "min_score": 80,
        "max_score": 100,
        "decision": "Auto approve",
        "reserve_pct": 0,
        "reserve_hold_days": 0,
        "payout": "T+7",
        "onboarding": "Live immediately.",
        "documents": [],
    },
    {
        "id": "approve_with_reserve",
        "min_score": 60,
        "max_score": 79,
        "decision": "Approve with reserve",
        "reserve_pct": 5,
        "reserve_hold_days": 90,
        "payout": "T+14",
        "onboarding": "Live immediately, rolling reserve applied.",
        "documents": [],
    },
    {
        "id": "manual_review",
        "min_score": 40,
        "max_score": 59,
        "decision": "Manual review",
        "reserve_pct": None,
        "reserve_hold_days": None,
        "payout": None,
        "onboarding": "Held pending document review.",
        "documents": [
            "Certificate of incorporation",
            "Government photo ID of the beneficial owner",
            "Bank account proof in the legal entity name",
        ],
    },
    {
        "id": "decline",
        "min_score": 0,
        "max_score": 39,
        "decision": "Decline",
        "reserve_pct": None,
        "reserve_hold_days": None,
        "payout": None,
        "onboarding": "Rejected.",
        "documents": [],
    },
]

BAND_SEVERITY = ["auto_approve", "approve_with_reserve", "manual_review", "decline"]


# ── Reason codes ────────────────────────────────────────────────────────────
# Machine codes are the contract. Prose is for humans and may be reworded
# between policy versions; a code never changes meaning inside a major version.
REASON_CODES = {
    "DOM_AGE_LT_30": "Domain registered less than 30 days ago.",
    "DOM_AGE_LT_180": "Domain registered less than 180 days ago.",
    "DOM_TERM_MINIMUM": "Domain registered for the one-year minimum term only.",
    "DOM_PRIVACY_PROXY": "Registrant identity is masked by a privacy proxy.",
    "TLD_HIGH_ABUSE": "Top-level domain sits in the highest abuse tier.",
    "TLD_ELEVATED_ABUSE": "Top-level domain sits in an elevated abuse tier.",
    "SITE_UNREACHABLE": "Root URL did not serve a page.",
    "SITE_HTTP_ERROR": "Root URL returned an error status.",
    "TLS_INVALID": "TLS certificate is missing, expired or fails validation.",
    "TLS_EXPIRING_SOON": "TLS certificate expires within 15 days.",
    "THIN_CONTENT": "Root page carries under 300 words, consistent with a shell site.",
    "PARKED_DOMAIN": "Root page matches a domain-parking template.",
    "SLOW_TTFB": "Time to first byte exceeds 1.5 seconds.",
    "NO_REFUND_POLICY": "No refund or cancellation policy page found.",
    "NO_TERMS_PAGE": "No terms of service page found.",
    "NO_PRIVACY_PAGE": "No privacy policy page found.",
    "NO_CONTACT_PAGE": "No contact page found.",
    "NO_PRICING_PAGE": "No pricing page found.",
    "NO_PROCESSOR_DETECTED": "No incumbent payment processor detected in page source.",
    "NO_PRICE_PUBLISHED": "No price is published anywhere on the crawled pages.",
    "NO_COMMERCIAL_SURFACE": (
        "Site publishes no policy page, no price and no payment processor — there is no "
        "commercial surface to underwrite."
    ),
    "HIGH_TICKET": "Highest listed price exceeds the high-ticket threshold.",
    "RECURRING_NO_CANCELLATION": "Recurring billing offered without cancellation terms.",
    "CATEGORY_RESTRICTED": "Inferred category is on the restricted list.",
    "CATEGORY_RESTRICTED_REVIEW": (
        "Site content reads as a restricted vertical, but too thinly to decline on."
    ),
    "CATEGORY_ELEVATED": "Inferred category is on the elevated-risk list.",
    "CATEGORY_MISMATCH": "Declared category does not match the category inferred from site content.",
    "CATEGORY_UNKNOWN": "Site content did not map to any category in the taxonomy.",
    "CATEGORY_UNREADABLE": (
        "Site carries too little text to establish what it sells."
    ),
    "RESTRICTED_KEYWORDS": "Restricted-vertical keywords present in site content.",
    "SAFEBROWSING_HIT": "Google Safe Browsing lists this domain.",
    "GEO_MISMATCH": "Declared country disagrees with hosting country and ccTLD.",
    "LEGAL_NAME_MISMATCH": "Declared legal name does not appear in site footer or policy pages.",
    "EMAIL_DOMAIN_MISMATCH": "Contact email is not on the merchant's own domain.",
    "LOW_CONFIDENCE": "Under 60% of policy weight could be computed for this run.",
}

# Codes that override the band the score would otherwise buy.
# "cap" means the decision cannot be better than the named band.
BAND_OVERRIDES = {
    "SAFEBROWSING_HIT": "decline",
    "CATEGORY_RESTRICTED": "decline",
    "PARKED_DOMAIN": "decline",
    "SITE_UNREACHABLE": "decline",
    # A live page that sells nothing is not a merchant. This fires only when the
    # crawl actually looked and found all of it absent: no refund, terms, privacy,
    # contact or pricing page, no processor in the source, no price and no
    # checkout language anywhere. A crawl that ran out of budget leaves those
    # signals unavailable rather than zero and cannot trip it.
    "NO_COMMERCIAL_SURFACE": "decline",
    "CATEGORY_RESTRICTED_REVIEW": "manual_review",
    "CATEGORY_MISMATCH": "manual_review",
    "NO_REFUND_POLICY": "approve_with_reserve",
    "TLS_INVALID": "manual_review",
    "LOW_CONFIDENCE": "manual_review",
}

# ── Runtime budgets ─────────────────────────────────────────────────────────
GLOBAL_TIMEOUT_S = 8.0
PER_CALL_TIMEOUT_S = 3.0
CACHE_TTL_S = 24 * 60 * 60
MAX_INTERNAL_LINKS = 15
CRAWL_WORKERS = 8
CONFIDENCE_FLOOR = 0.60  # below this the run is capped at manual review


def band_for_score(score: float) -> dict:
    for band in DECISION_BANDS:
        if score >= band["min_score"]:
            return band
    return DECISION_BANDS[-1]


def apply_overrides(band_id: str, codes: list[str]) -> tuple[str, list[str]]:
    """Return the worst of the scored band and any override caps the codes force."""
    worst = BAND_SEVERITY.index(band_id)
    applied = []
    for code in codes:
        cap = BAND_OVERRIDES.get(code)
        if cap is None:
            continue
        idx = BAND_SEVERITY.index(cap)
        if idx > worst:
            worst = idx
            applied.append(code)
        elif idx == worst and band_id != cap:
            applied.append(code)
    return BAND_SEVERITY[worst], applied


def band_by_id(band_id: str) -> dict:
    for band in DECISION_BANDS:
        if band["id"] == band_id:
            return band
    return DECISION_BANDS[-1]
