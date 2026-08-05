"""
Commercial legitimacy — 20 points.

Does this merchant publish the pages a real seller has to publish.

Refund policy carries 8 of the 20 points, twice any other page in the category.
The absence of a stated refund or cancellation policy is the strongest single
predictor of chargeback volume: when a customer cannot find how to get their
money back from the merchant, they ask their bank instead, and that arrives as
a dispute rather than a refund.
"""
from __future__ import annotations

from .base import ok, unavailable

# key -> (signal key, human noun, reason code, minimum words to count as real)
PAGE_SIGNALS = {
    "refund":  ("refund_policy",  "refund or cancellation policy", "NO_REFUND_POLICY", 60),
    "terms":   ("terms_page",     "terms of service page",         "NO_TERMS_PAGE",    80),
    "privacy": ("privacy_page",   "privacy policy page",           "NO_PRIVACY_PAGE",  80),
    "contact": ("contact_page",   "contact page",                  "NO_CONTACT_PAGE",  20),
    "pricing": ("pricing_page",   "pricing page",                  "NO_PRICING_PAGE",  20),
}


def _page_signal(cls: str, bundle: dict) -> "Signal":
    signal_key, noun, code, min_words = PAGE_SIGNALS[cls]

    if not bundle.get("root_ok"):
        return unavailable(signal_key, f"No page was retrieved, so the {noun} could not be looked for.")

    fetched = bundle.get("pages", {}).get(cls)
    linked = bundle.get("pages_found", {}).get(cls)

    if fetched and fetched["word_count"] >= min_words:
        return ok(signal_key, fetched["url"], 100,
                  f"A {noun} is published at {fetched['url']} with {fetched['word_count']} words of substantive text.",
                  url=fetched["url"], word_count=fetched["word_count"])

    if fetched:
        return ok(signal_key, f"{fetched['url']} (thin)", 45,
                  f"A {noun} exists at {fetched['url']} but carries only {fetched['word_count']} words, too little to state real terms.",
                  [code], url=fetched["url"], word_count=fetched["word_count"], thin=True)

    if linked:
        # Linked but not retrieved: the merchant published it, our crawl ran out
        # of budget. Credit the merchant, flag the uncertainty.
        return ok(signal_key, f"{linked} (linked, not retrieved)", 75,
                  f"The site links to a {noun} at {linked}, but the crawl budget ran out before it could be read.",
                  url=linked, verified=False)

    if bundle.get("crawl_truncated"):
        return unavailable(signal_key,
                           f"The 8 second budget expired before the crawl could rule out a {noun}.")

    return ok(signal_key, "not found", 0,
              f"No {noun} was found in {bundle.get('links_discovered', 0)} internal links from the homepage.",
              [code], searched_links=bundle.get("links_discovered", 0))


def refund_policy(bundle: dict) -> "Signal":
    return _page_signal("refund", bundle)


def terms_page(bundle: dict) -> "Signal":
    return _page_signal("terms", bundle)


def privacy_page(bundle: dict) -> "Signal":
    return _page_signal("privacy", bundle)


def contact_page(bundle: dict) -> "Signal":
    signal = _page_signal("contact", bundle)
    # A published support address is a contact channel even without a contact page.
    if signal.status == "ok" and signal.normalized == 0 and bundle.get("emails"):
        return ok("contact_page", f"email only: {bundle['emails'][0]}", 55,
                  f"There is no contact page, but a reachable address ({bundle['emails'][0]}) is published on the site.",
                  emails=bundle["emails"][:3])
    return signal


def pricing_page(bundle: dict) -> "Signal":
    return _page_signal("pricing", bundle)
