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


def category_tier(bundle: dict, inference: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("category_tier", "No page content was retrieved, so the category could not be inferred.")

    category = inference.get("category")
    if not category:
        words = bundle.get("word_count", 0)
        if words < 20:
            return unavailable("category_tier", "The page carried too little text to infer a category from.")
        return ok("category_tier", "unclassified", 50,
                  "The site's own text does not map to any category in the acceptance taxonomy, so it cannot be placed on the risk ladder.",
                  ["CATEGORY_UNKNOWN"], ranked=inference.get("ranked", []))

    label = LABEL_BY_CATEGORY[category]
    tier = TIER_BY_CATEGORY[category]
    evidence = ", ".join(inference.get("hits", [])[:4]) or "keyword profile"

    if tier == TIER_RESTRICTED:
        return ok("category_tier", f"{label} (restricted)", 5,
                  f"Site content reads as {label}, which is on the restricted list and cannot be boarded (matched: {evidence}).",
                  ["CATEGORY_RESTRICTED"], category=category, tier=tier,
                  hits=inference.get("hits", []), ranked=inference.get("ranked", []))
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

    hits = scan_restricted_keywords(bundle.get("combined_text", ""))

    if not hits:
        return ok("restricted_keywords", "0 hits", 100,
                  "No restricted-vertical terms appear anywhere in the crawled content.", hits=[])
    if len(hits) <= 2:
        return ok("restricted_keywords", f"{len(hits)} hits: {', '.join(hits)}", 45,
                  f"Restricted-vertical terms appear in the content ({', '.join(hits)}), which may be incidental but needs a human to read the page.",
                  ["RESTRICTED_KEYWORDS"], hits=hits)
    return ok("restricted_keywords", f"{len(hits)} hits: {', '.join(hits[:5])}", 0,
              f"{len(hits)} restricted-vertical terms appear in the content ({', '.join(hits[:5])}), too many to be incidental.",
              ["RESTRICTED_KEYWORDS"], hits=hits)
