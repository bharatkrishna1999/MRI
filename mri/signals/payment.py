"""
Payment surface — 15 points.

Who processes this merchant's payments today, how large the ticket is, and
whether billing recurs. An incumbent processor already accepting the merchant is
third-party underwriting we get for free. Ticket size and recurrence are the two
inputs that decide how much money is exposed when a dispute lands.
"""
from __future__ import annotations

import re

from .base import ok, unavailable

# Fingerprints left in page source by processors that underwrite merchants
# themselves. Presence means somebody else already ran KYC on this business.
PROCESSOR_FINGERPRINTS = {
    "Stripe": [r"js\.stripe\.com", r"stripe\.com/v3", r"data-stripe", r"stripe\.js",
               r"checkout\.stripe\.com", r"buy\.stripe\.com"],
    "Paddle": [r"cdn\.paddle\.com", r"paddle\.js", r"paddle_button", r"buy\.paddle\.com",
               r"checkout\.paddle\.com"],
    "Lemon Squeezy": [r"lemonsqueezy\.com", r"lmsqueezy\.com", r"lemon\.js"],
    "PayPal": [r"paypal\.com/sdk", r"paypalobjects\.com", r"paypal\.me",
               r"www\.paypal\.com/cgi-bin", r"paypal-button"],
    "Polar": [r"polar\.sh", r"api\.polar\.sh", r"buy\.polar\.sh"],
    "Gumroad": [r"gumroad\.com", r"gum\.co", r"gumroad-product-embed"],
}

_PROCESSOR_RE = {
    name: re.compile("|".join(patterns), re.I)
    for name, patterns in PROCESSOR_FINGERPRINTS.items()
}

# Currency-prefixed and currency-suffixed amounts, plus common plan pricing.
_PRICE_RE = re.compile(
    r"(?:(?P<sym>[$£€₹])\s?(?P<amt1>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?))"
    r"|(?:(?P<amt2>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s?(?P<code>USD|EUR|GBP|INR|AUD|CAD)\b)",
    re.I,
)

RECURRING_PATTERNS = [
    r"per\s+month", r"/\s?mo\b", r"/\s?month", r"monthly", r"per\s+year", r"/\s?yr\b",
    r"/\s?year", r"annually", r"billed\s+(monthly|annually|yearly)", r"subscription",
    r"recurring", r"auto[\s-]?renew", r"renews\s+automatically", r"free\s+trial",
    r"cancel\s+anytime", r"per\s+seat", r"per\s+user\s?/",
]
_RECURRING_RE = re.compile("|".join(RECURRING_PATTERNS), re.I)

CANCELLATION_PATTERNS = [
    r"cancel\s+anytime", r"cancel\s+(your\s+)?subscription", r"cancellation\s+policy",
    r"how\s+to\s+cancel", r"refund", r"money[\s-]back", r"end\s+your\s+subscription",
]
_CANCELLATION_RE = re.compile("|".join(CANCELLATION_PATTERNS), re.I)

# Language that only appears when a site is actually trying to take money. Kept
# deliberately transactional: a nav link reading "Shop" over a page that says
# "coming soon" is not a checkout, and treating it as one is how a brochure gets
# credited with a payment surface it does not have.
CHECKOUT_PATTERNS = [
    r"add\s+to\s+(cart|bag|basket)", r"\bcheckout\b", r"\bbuy\s+now\b", r"shopping\s+cart",
    r"\border\s+now\b", r"proceed\s+to\s+(payment|checkout)", r"\bsubscribe\b",
    r"start\s+(your\s+)?(free\s+)?trial", r"book\s+(a\s+)?(demo|call)", r"\bget\s+started\b",
    r"request\s+(a\s+)?quote", r"contact\s+sales", r"\bplace\s+(your\s+)?order\b",
    r"\bpay\s+now\b", r"\bview\s+plans?\b",
]
_CHECKOUT_RE = re.compile("|".join(CHECKOUT_PATTERNS), re.I)

HIGH_TICKET_THRESHOLD = 2000


def commercial_surface(bundle: dict) -> dict:
    """
    Is there any evidence at all that this site can take money?

    Four independent marks: a published price, a processor fingerprint in the
    page source, transactional language in the copy, and a pricing page. A site
    carrying none of the four does not have a cheap commercial surface or a
    hidden one — it has none, and the signals that describe *how* it charges
    have nothing to describe.

    This is the shared definition behind three things: whether a missing price
    reads as enterprise sales or as nothing for sale, whether the absence of
    subscription wording is a billing model or an absence of billing, and the
    NO_COMMERCIAL_SURFACE finding itself.
    """
    text = bundle.get("combined_text", "")
    html = bundle.get("combined_html", "")
    pages = bundle.get("pages") or {}
    found = bundle.get("pages_found") or {}

    prices = _extract_prices(text)
    processors = [name for name, rx in _PROCESSOR_RE.items() if rx.search(html)]
    checkout = _CHECKOUT_RE.search(text)

    marks = {
        "price": bool(prices),
        "processor": bool(processors),
        "checkout": bool(checkout),
        "pricing_page": "pricing" in pages or "pricing" in found,
    }
    return {
        "marks": marks,
        "any": any(marks.values()),
        "present": sorted(k for k, v in marks.items() if v),
        "prices": prices,
        "processors": processors,
        "checkout_phrase": checkout.group(0).strip() if checkout else "",
    }


def processor(bundle: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("processor", "No page source was retrieved, so the checkout could not be fingerprinted.")

    html = bundle.get("combined_html", "")
    found = [name for name, rx in _PROCESSOR_RE.items() if rx.search(html)]

    if found:
        listed = ", ".join(found)
        return ok("processor", listed, 100,
                  f"{listed} is already embedded in the checkout, which means an incumbent processor has underwritten this merchant.",
                  detected=found)

    return ok("processor", "none detected", 25,
              "No incumbent processor fingerprint appears in the page source, so no other provider has vouched for this merchant.",
              ["NO_PROCESSOR_DETECTED"], detected=[])


def _extract_prices(text: str) -> list[float]:
    prices = []
    for match in _PRICE_RE.finditer(text):
        raw = match.group("amt1") or match.group("amt2")
        if not raw:
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        # Filter out years and obvious non-prices picked up by a loose regex.
        if 1900 <= value <= 2100 and value == int(value) and not match.group("sym"):
            continue
        if 0 < value <= 500_000:
            prices.append(value)
    return prices


def price_point(bundle: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("price_point", "No page content was retrieved, so no prices could be read.")

    text = bundle.get("combined_text", "")
    prices = _extract_prices(text)
    if not prices:
        # Not neutral, and how far from neutral depends on what else is there.
        # A site with a pricing page, a processor or a "contact sales" button and
        # no number on it is an enterprise merchant quoting privately; we cannot
        # size the ticket, but somebody is plainly selling something. A site with
        # no price, no pricing page, no processor and no checkout language is not
        # withholding a number — it has nothing to charge for, and 40/100 for
        # that was the engine crediting an applicant for the absence of evidence.
        surface = commercial_surface(bundle)
        if surface["any"]:
            return ok("price_point", "no price found", 40,
                      "No price is published anywhere on the crawled pages, so there is nothing to size "
                      f"this merchant's ticket exposure against, though the site does sell "
                      f"({', '.join(surface['present'])}).",
                      prices_found=0, surface=surface["present"])
        return ok("price_point", "nothing offered for sale", 15,
                  "No price, no pricing page, no processor and no checkout language appears anywhere on "
                  "the crawled pages. There is no ticket to size because nothing on this site is for sale.",
                  ["NO_PRICE_PUBLISHED"], prices_found=0, surface=[])

    highest = max(prices)
    if highest <= 100:
        score, codes = 100, []
        band_text = "low ticket, where disputes are cheap to absorb"
    elif highest <= 500:
        score, codes = 85, []
        band_text = "mid ticket"
    elif highest <= HIGH_TICKET_THRESHOLD:
        score, codes = 60, []
        band_text = "high ticket, where a single dispute is material"
    else:
        score, codes = 30, ["HIGH_TICKET"]
        band_text = f"above the {HIGH_TICKET_THRESHOLD:,} high-ticket threshold, where one dispute can exceed a week of revenue"

    return ok("price_point", f"{highest:,.2f}", score,
              f"The highest listed price is {highest:,.2f}, {band_text}.",
              codes, highest=highest, count=len(prices),
              median=sorted(prices)[len(prices) // 2])


def recurring_billing(bundle: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("recurring_billing", "No page content was retrieved, so billing language could not be read.")

    text = bundle.get("combined_text", "")
    recurring_match = _RECURRING_RE.search(text)

    if not recurring_match:
        # "No subscription wording" is only evidence of a one-time billing model
        # if there is a billing model at all. On a site that offers nothing for
        # sale there is no recurrence question to answer, and answering it anyway
        # paid a brochure site three of its four payment-surface points for
        # having no checkout. Abstain instead: an unmeasurable signal leaves the
        # denominator rather than scoring, which is this engine's whole contract.
        surface = commercial_surface(bundle)
        if not surface["any"]:
            return unavailable("recurring_billing",
                               "The site offers nothing for sale — no price, no pricing page, no processor "
                               "and no checkout language — so there is no billing model to describe as "
                               "recurring or one-time.",
                               raw="no billing surface")
        return ok("recurring_billing", "one-time", 75,
                  "No recurring billing language appears on the site, so this reads as one-time purchases with no renewal disputes.",
                  recurring=False, surface=surface["present"])

    has_cancellation = bool(_CANCELLATION_RE.search(text)) or "refund" in bundle.get("pages", {})
    if has_cancellation:
        return ok("recurring_billing", f"recurring ('{recurring_match.group(0)}'), cancellation stated", 100,
                  f"The site sells recurring billing ('{recurring_match.group(0).strip()}') and states how to cancel, which is the combination that keeps renewals out of dispute.",
                  recurring=True, cancellation=True)

    return ok("recurring_billing", f"recurring ('{recurring_match.group(0)}'), no cancellation terms", 35,
              f"The site bills recurringly ('{recurring_match.group(0).strip()}') but never says how to cancel, which is the single most common source of subscription chargebacks.",
              ["RECURRING_NO_CANCELLATION"], recurring=True, cancellation=False)
