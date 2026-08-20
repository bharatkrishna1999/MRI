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

# Below this the homepage did not parse into anything worth calling content, and
# the likeliest reason is a client-rendered app our fetcher cannot execute rather
# than a merchant with nothing to say. Matches the floor content_depth already
# uses to call a page a placeholder.
READABLE_WORD_FLOOR = 50

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


def no_commercial_surface(signals: list, bundle: dict) -> bool:
    """
    Is this a live page with no business behind it at all?

    Five missing policy pages, no processor, no price and no checkout language
    are not eight independent findings that a weighted average should thin out
    against each other. They are one finding — nobody is selling anything here —
    observed eight ways, and averaging correlated evidence is exactly how a
    brochure site keeps the free points that a valid certificate, a fast first
    byte and an unparked homepage hand to any domain bought this morning.

    This caps at decline on its own, so what it takes to trip matters more than
    what it does once tripped. Three guards, and every one of them exists to
    keep "we could not read this site" from being mistaken for "this site has
    nothing on it":

    * **The crawl must have read a page.** A client-rendered application serves
      a static shell — `<div id="root"></div>` and a script tag — and injects
      every word of copy, every nav link and every policy link after load. Our
      fetcher does not run JavaScript, so that shell parses to zero words and
      zero anchors, and *every* absence below is then guaranteed rather than
      observed. Requiring readable text and at least one internal link is what
      separates a merchant who published nothing from an app we cannot render.
      Without this guard a funded SaaS on React declines automatically, which is
      a far worse error than holding a brochure for review.
    * **The crawl must have looked.** Every one of the five page signals has to
      have scored zero, meaning it searched the site's links and found nothing.
      A crawl that ran out of budget leaves those signals unavailable instead,
      and unavailable never trips this.
    * **One mark of commerce anywhere clears it.** One published price, one
      processor fingerprint, one "add to cart", one pricing page, or a support
      address on the homepage (which scores the contact page at 55, not zero)
      and this does not fire.

    That leaves the case it is meant for: a site we read in full, that publishes
    no terms, no refund policy, no privacy policy, no way to contact anyone, no
    price, and no means of taking money. That is not a merchant with a weak
    application. It is not a merchant.
    """
    from .base import OK
    from .payment import commercial_surface

    if not bundle.get("root_ok"):
        return False  # SITE_UNREACHABLE covers this; do not double-count it.

    # Did we actually read a site, or just fail to render one? "No refund policy
    # found in 0 internal links" is not a finding about the merchant.
    if bundle.get("word_count", 0) < READABLE_WORD_FLOOR:
        return False
    if bundle.get("links_discovered", 0) < 1:
        return False

    by_key = {s.key: s for s in signals}
    for _, (signal_key, _, _, _) in PAGE_SIGNALS.items():
        signal = by_key.get(signal_key)
        if signal is None or signal.status != OK or signal.normalized != 0:
            return False

    return not commercial_surface(bundle)["any"]
