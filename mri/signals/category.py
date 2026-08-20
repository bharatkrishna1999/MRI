"""
Category risk — 15 points.

What the site actually sells, inferred from its own text, scored against the
acceptance taxonomy.

The rule this module exists to enforce: the merchant's declared category is
never an input to a score. It is used for exactly one thing, comparing it
against the inferred category to produce a mismatch flag, because a declared
category is a claim by the applicant and the applicant is the party with an
incentive to lie. If the applicant declares nothing, the mismatch signal returns
unavailable and drops out of the denominator; it never becomes free points.
"""
from __future__ import annotations

from ..taxonomy import (
    LABEL_BY_CATEGORY,
    TIER_BY_CATEGORY,
    TIER_ELEVATED,
    TIER_RESTRICTED,
    TIER_STANDARD,
    infer_category,
    scan_restricted_keywords,
)
from .base import ok, unavailable

# Words of offering copy, across the whole crawl, below which the classifier was
# starved rather than defeated. A merchant in a vertical the taxonomy genuinely
# misses still writes a few hundred words about what it does.
MIN_CLASSIFIABLE_WORDS = 200


def category_tier(bundle: dict, inference: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("category_tier", "No page content was retrieved, so the category could not be inferred.")

    category = inference.get("category")
    if not category:
        words = bundle.get("word_count", 0)
        corpus = len((bundle.get("category_text") or bundle.get("combined_text", "")).split())
        if words < 20:
            return unavailable("category_tier", "The page carried too little text to infer a category from.")
        # "No category matched" covers two findings that are not the same size,
        # and scoring both at 50 gave the worse one the benefit of the doubt
        # earned by the better one.
        #
        # A site we can see is trading — a price, a processor, a checkout — that
        # matches nothing is a bounded uncertainty. We know it is a shop; we
        # cannot place it on the risk ladder. That is a gap in our taxonomy as
        # often as it is a fact about the merchant, and it is what 50 is for.
        # A one-page print shop with real prices and a real Stripe key is in this
        # case, and must not be punished for a taxonomy that has no row for it.
        #
        # A site that matches nothing, says almost nothing, and shows no sign of
        # selling anything is not an unplaceable merchant. It is a site where we
        # could not establish what — or whether — the business is, which is
        # strictly more uncertain than being in a vertical we do not carry.
        from .payment import commercial_surface

        surface = commercial_surface(bundle)
        if corpus < MIN_CLASSIFIABLE_WORDS and not surface["any"]:
            return ok("category_tier", f"unreadable ({corpus} words, nothing for sale)", 15,
                      f"The whole site carries {corpus} words describing what it offers and shows no price, "
                      f"processor or checkout anywhere. There is not enough here to establish what this "
                      f"merchant sells, or that it sells anything.",
                      ["CATEGORY_UNKNOWN", "CATEGORY_UNREADABLE"], corpus_words=corpus,
                      floor=MIN_CLASSIFIABLE_WORDS, surface=[],
                      ranked=inference.get("ranked", []))
        return ok("category_tier", "unclassified", 50,
                  f"The site's {corpus} words of offering copy do not map to any category in the acceptance "
                  f"taxonomy, so it cannot be placed on the risk ladder — but it is visibly trading "
                  f"({', '.join(surface['present']) or 'content is substantial enough to read'}), which "
                  f"makes this a gap in the taxonomy rather than an empty storefront.",
                  ["CATEGORY_UNKNOWN"], corpus_words=corpus, surface=surface["present"],
                  ranked=inference.get("ranked", []))

    label = LABEL_BY_CATEGORY[category]
    tier = TIER_BY_CATEGORY[category]
    evidence = ", ".join(inference.get("hits", [])[:4]) or "keyword profile"

    if tier == TIER_RESTRICTED:
        if not inference.get("confident", True):
            # Thin evidence. A restricted call is the one finding in this policy
            # that declines an application by itself, and a decline that rests on
            # a couple of incidental words is a decline we cannot defend to the
            # merchant. Route it to a reviewer who can read the page instead.
            runner_up = inference.get("runner_up", 0)
            return ok("category_tier", f"{label} (restricted, unconfirmed)", 40,
                      f"Site content weakly reads as {label} on {inference.get('score', 0)} keyword "
                      f"match(es) (matched: {evidence}), no stronger than the {runner_up} match(es) "
                      f"for an acceptable category, so it is sent for human review rather than "
                      f"declined on the keyword scan alone.",
                      ["CATEGORY_RESTRICTED_REVIEW"], category=category, tier=tier,
                      confident=False, hits=inference.get("hits", []),
                      ranked=inference.get("ranked", []))
        return ok("category_tier", f"{label} (restricted)", 5,
                  f"Site content reads as {label}, which is on the restricted list and cannot be boarded (matched: {evidence}).",
                  ["CATEGORY_RESTRICTED"], category=category, tier=tier,
                  confident=True, hits=inference.get("hits", []),
                  ranked=inference.get("ranked", []))
    if tier == TIER_ELEVATED:
        return ok("category_tier", f"{label} (elevated)", 55,
                  f"Site content reads as {label}, an elevated-risk vertical that boards only with a reserve (matched: {evidence}).",
                  ["CATEGORY_ELEVATED"], category=category, tier=tier,
                  hits=inference.get("hits", []), ranked=inference.get("ranked", []))
    return ok("category_tier", f"{label} (standard)", 100,
              f"Site content reads as {label}, a standard digital category (matched: {evidence}).",
              category=category, tier=tier, hits=inference.get("hits", []),
              ranked=inference.get("ranked", []))


def category_mismatch(declared_category: str | None, inference: dict) -> "Signal":
    if not declared_category:
        return unavailable("category_mismatch",
                           "The applicant declared no category, so there is nothing to compare the inferred category against.")

    inferred = inference.get("category")
    declared_label = LABEL_BY_CATEGORY.get(declared_category, declared_category)

    if not inferred:
        return unavailable("category_mismatch",
                           f"The site's category could not be inferred, so the declared '{declared_label}' cannot be verified either way.")

    inferred_label = LABEL_BY_CATEGORY[inferred]
    if inferred == declared_category:
        return ok("category_mismatch", "match", 100,
                  f"The applicant declared {declared_label} and the site content reads as {inferred_label}.",
                  declared=declared_category, inferred=inferred)

    declared_tier = TIER_BY_CATEGORY.get(declared_category, TIER_STANDARD)
    inferred_tier = TIER_BY_CATEGORY[inferred]

    if declared_tier == inferred_tier:
        return ok("category_mismatch", f"adjacent: declared {declared_label}, inferred {inferred_label}", 70,
                  f"The applicant declared {declared_label} but the site reads as {inferred_label}; different categories, same risk tier, so this is a classification difference rather than a misrepresentation.",
                  declared=declared_category, inferred=inferred)

    # Understating risk is the fraud pattern. Overstating it is not.
    tiers = [TIER_STANDARD, TIER_ELEVATED, TIER_RESTRICTED]
    understated = tiers.index(inferred_tier) > tiers.index(declared_tier)
    if understated:
        return ok("category_mismatch", f"mismatch: declared {declared_label}, inferred {inferred_label}", 0,
                  f"The applicant declared {declared_label} but the site content reads as {inferred_label}, a higher risk tier. Declaring a safer category than the site actually operates is the single clearest fraud signal in this system.",
                  ["CATEGORY_MISMATCH"], declared=declared_category, inferred=inferred,
                  understated=True)

    return ok("category_mismatch", f"declared {declared_label}, inferred {inferred_label}", 60,
              f"The applicant declared {declared_label}, a higher risk tier than the {inferred_label} the site reads as. Overstating risk is not a fraud pattern, but the declaration is still unverified.",
              declared=declared_category, inferred=inferred, understated=False)


def restricted_keywords(bundle: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("restricted_keywords", "No page content was retrieved, so the keyword scan could not run.")

    # Same corpus as the category inference: what the merchant sells, not the
    # acceptable-use page listing what it refuses to sell.
    corpus = bundle.get("category_text") or bundle.get("combined_text", "")
    words = len(corpus.split())
    hits = scan_restricted_keywords(corpus)

    if not hits:
        # A clean scan clears a merchant only if there was something to scan. On
        # a near-empty site "no banned words found" is arithmetic about the page
        # length, not a finding about the business, and it was paying full marks
        # for it. The asymmetry is deliberate: hits are still scored on any
        # corpus, because a hit is evidence and an absence is not. This can only
        # ever remove unearned credit, never manufacture a decline.
        if words < MIN_CLASSIFIABLE_WORDS:
            return unavailable("restricted_keywords",
                               f"The crawled site carries {words} words in total, too few for a clean keyword "
                               f"scan to say anything about which vertical this merchant is in.",
                               raw=f"{words} words scanned")
        return ok("restricted_keywords", "0 hits", 100,
                  f"No restricted-vertical terms appear anywhere in the {words} words of crawled content.",
                  hits=[])
    if len(hits) <= 2:
        return ok("restricted_keywords", f"{len(hits)} hits: {', '.join(hits)}", 45,
                  f"Restricted-vertical terms appear in the content ({', '.join(hits)}), which may be incidental but needs a human to read the page.",
                  ["RESTRICTED_KEYWORDS"], hits=hits)
    return ok("restricted_keywords", f"{len(hits)} hits: {', '.join(hits[:5])}", 0,
              f"{len(hits)} restricted-vertical terms appear in the content ({', '.join(hits[:5])}), too many to be incidental.",
              ["RESTRICTED_KEYWORDS"], hits=hits)
