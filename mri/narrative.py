"""
Two pieces of prose for people who do not read scoring tables.

`summarise` returns both:

  business — a short paragraph saying what this merchant appears to be, built
             from the merchant's own words (title, meta description, about page)
             plus what the crawl and the taxonomy actually found.
  why      — the same decision the engine just made, said in plain English:
             what we are doing, what it costs the merchant, what helped, what
             hurt, and what happens next.

Everything here is derived from evidence the engine already gathered. Nothing in
this module makes a network call, and nothing in it can change a decision — it
runs after `decide` and only reads. That separation is deliberate: prose that
can move a verdict is not an explanation, it is a second scorer.

The signal-by-signal wording lives in PLAIN. The `reason` string each signal
already emits is written for an underwriter; these are written for whoever has
to tell the merchant. They say the same thing.
"""
from __future__ import annotations

from .policy import REASON_CODES
from .taxonomy import LABEL_BY_CATEGORY, TIER_BY_CATEGORY

# ── Wording tables ──────────────────────────────────────────────────────────
VERDICT = {
    "auto_approve": "We can take this business on, with no conditions attached.",
    "approve_with_reserve": (
        "We can take this business on, but we hold back a slice of its money for a while "
        "as cover."
    ),
    "manual_review": (
        "Nobody has said yes or no yet. A person needs to look at this one before it can "
        "go live."
    ),
    "decline": "We are not taking this business on.",
}

BAND_PLAIN = {
    "auto_approve": "an unconditional yes",
    "approve_with_reserve": "a yes with money held back",
    "manual_review": "a hold for someone to review by hand",
    "decline": "a no",
}

MONEY = {
    "auto_approve": "They are paid seven days after each sale, and we keep none of it back.",
    "approve_with_reserve": (
        "They are paid fourteen days after each sale, and we keep 5% of it for 90 days in "
        "case customers come back asking for refunds."
    ),
    "manual_review": (
        "Nothing goes live and no money moves until the documents we have asked for come "
        "back and check out."
    ),
    "decline": "No account is opened and no payments are processed.",
}

NEXT_STEP = {
    "auto_approve": (
        "Switch the account on. Normal monitoring applies once money starts moving."
    ),
    "approve_with_reserve": (
        "Switch the account on with the reserve applied, then look at it again after 90 "
        "days against what refunds and chargebacks actually cost."
    ),
    "manual_review": (
        "Ask the merchant for their certificate of incorporation, the owner's photo ID and "
        "proof of a bank account in the company's name — then have someone open the site "
        "and make the call."
    ),
    "decline": (
        "Tell the merchant no and give them the reasons listed on this page. Keep the "
        "record; it is the evidence if they contest it."
    ),
}

TIER_PHRASE = {
    "standard": "an ordinary line of business for a merchant of record",
    "elevated": "a line of business that carries real refund and chargeback exposure",
    "restricted": "a line of business a merchant of record cannot board at all",
}

# The findings that overrule the score. Said the way you would say them to
# somebody who has never seen the policy.
DECISIVE = {
    "SAFEBROWSING_HIT": (
        "Google's dangerous-site list names this domain. That ends it on its own, no matter "
        "how good everything else looks."
    ),
    "CATEGORY_RESTRICTED": (
        "The site's own pages read as a line of business we do not board at all. That is a "
        "rule, not a score — nothing else on the page can buy it back."
    ),
    "PARKED_DOMAIN": (
        "There is no real website here. The address shows the holding page a parking "
        "service puts up when nobody has built anything yet."
    ),
    "SITE_UNREACHABLE": (
        "The website did not load at all, so there was nothing to look at. We do not board "
        "a business we cannot see."
    ),
    "CATEGORY_RESTRICTED_REVIEW": (
        "A few words on the site hint at a line of business we do not board, but too faintly "
        "to refuse on. Someone should read the storefront and decide."
    ),
    "CATEGORY_MISMATCH": (
        "What the applicant told us they sell is not what their website sells. That gap "
        "always goes to a person, because it is the clearest sign of an application that is "
        "not being straight with us."
    ),
    "NO_REFUND_POLICY": (
        "There is no refund or cancellation page anywhere on the site. A customer who cannot "
        "find how to get their money back asks their bank instead, and that arrives as a "
        "chargeback — so this can never be an automatic yes, however clean the rest is."
    ),
    "TLS_INVALID": (
        "The site's security certificate is missing or broken, so card details typed into it "
        "would not be protected."
    ),
    "LOW_CONFIDENCE": (
        "Too little of the check could actually be completed this time for the number to "
        "mean anything, so it goes to a person rather than being guessed at."
    ),
}

# Per signal: how to say it when it went well, and when it went badly.
# `{raw}` is the value the signal actually observed.
PLAIN = {
    "domain_age": (
        "The web address has been registered for {raw}, long past the window fraud "
        "domains live in.",
        "The web address is new — {raw}. Almost all payment fraud is run from addresses "
        "registered in the last few months.",
    ),
    "registration_term": (
        "They have paid to keep the address for {raw}, which someone planning to disappear "
        "does not bother doing.",
        "The address is paid up for the one-year minimum only — the cheapest possible "
        "commitment to the name.",
    ),
    "privacy_proxy": (
        "The owner's details are published in the public registration record, so we can "
        "check them against the applicant.",
        "The owner's identity is hidden behind a privacy service, so we cannot check who "
        "actually owns the address.",
    ),
    "tld_abuse": (
        "The address ends in {raw}, which is not one of the endings associated with abuse.",
        "The address ends in {raw} — one of the domain endings most used for spam and fraud.",
    ),
    "http_root": (
        "The website loads normally.",
        "The website did not serve a page properly ({raw}).",
    ),
    "tls": (
        "The site has a valid security certificate, so anything a customer types is encrypted.",
        "The security certificate is missing, expired or does not check out ({raw}).",
    ),
    "content_depth": (
        "There is a substantial amount of real writing on the site ({raw} on the home page), "
        "which takes time and effort to produce.",
        "There is very little writing on the site — {raw} on the home page. That is thin in "
        "the way a site built overnight to take card payments is thin.",
    ),
    "parked": (
        "It is a working storefront, not a placeholder.",
        "The page matches a domain-parking template — the 'this domain is for sale' kind.",
    ),
    "ttfb": (
        "The site responds quickly ({raw}).",
        "The site is slow to respond ({raw}), which usually means neglected or overloaded "
        "hosting.",
    ),
    "refund_policy": (
        "There is a published refund or cancellation policy. This is the single most "
        "important page on the list.",
        "There is no refund or cancellation policy to be found. Customers who cannot find "
        "how to get their money back ask their bank instead.",
    ),
    "terms_page": (
        "Terms of service are published.",
        "No terms of service page could be found.",
    ),
    "privacy_page": (
        "A privacy policy is published.",
        "No privacy policy could be found.",
    ),
    "contact_page": (
        "There is a way to contact a human.",
        "There is no contact page — nowhere for a customer to complain before they call "
        "their bank.",
    ),
    "pricing_page": (
        "Prices are published openly.",
        "No pricing page could be found, so what this business charges is not stated "
        "anywhere we could see.",
    ),
    "processor": (
        "{raw} already handles their payments, which means another payments company has "
        "already run its own checks on this business.",
        "No known payment provider appears anywhere in the site's code, so nobody else has "
        "vetted this business yet.",
    ),
    "price_point": (
        "The most expensive thing on sale is {raw}, small enough that a single dispute is "
        "cheap to absorb.",
        "The most expensive thing on sale is {raw}. A single dispute at that size is real "
        "money.",
    ),
    "recurring_billing": (
        "It bills on a subscription and says plainly how to cancel — the combination that "
        "keeps renewals out of dispute.",
        "It bills people on a repeating subscription but never says how to cancel. That is "
        "the most common cause of subscription chargebacks there is.",
    ),
    "category_tier": (
        "What it sells — {raw} — is ordinary business for us.",
        "What it sells reads as {raw}, which is not a business we take on lightly.",
    ),
    "category_mismatch": (
        "What the applicant said they sell matches what their site sells.",
        "What the applicant said they sell does not match what their site sells.",
    ),
    "restricted_keywords": (
        "None of the words that mark a banned line of business appear on the site.",
        "Words that mark a banned line of business appear in the site's own copy ({raw}).",
    ),
    "safe_browsing": (
        "No threat list flags this domain.",
        "A threat feed lists this domain as dangerous.",
    ),
    "geo_consistency": (
        "The country on the application agrees with where the site is hosted.",
        "The country on the application does not agree with where the site is hosted or "
        "what the domain ending suggests.",
    ),
    "legal_name_match": (
        "The company name on the application appears on the merchant's own site.",
        "The company name on the application appears nowhere on the merchant's own site.",
    ),
    "email_domain_match": (
        "The contact email is on the merchant's own domain.",
        "The contact email is not on the merchant's own domain — it is a free mailbox or "
        "somebody else's.",
    ),
}

GOOD_AT = 70    # normalised score at or above which a signal counts as helping
BAD_UNDER = 50  # below which it counts as hurting
MAX_POINTS = 4  # most helped/hurt lines we will show; past that nobody reads on


def _signal_map(signals: list[dict]) -> dict:
    return {s["key"]: s for s in signals}


NO_PRICE = (
    "No price is published anywhere on the site, so there is no way to size how much a "
    "single dispute would cost us."
)

# A domain nobody has registered has no age to describe. Running it through the
# domain_age wording gave "The web address is new — not registered", which reads
# as a contradiction and understates what was actually found.
NOT_REGISTERED = (
    "The web address is not registered to anybody. There is no ownership record for it at "
    "all, which means there is no website and no business here to take on."
)


def _raw_for(signal: dict):
    """
    What to substitute for {raw}. Usually the value the signal recorded, but a
    few signals record something built for a table cell rather than a sentence.
    """
    key, detail = signal["key"], signal.get("detail") or {}
    if key == "domain_age" and detail.get("days") is not None and detail["days"] >= 365:
        return f"{detail['days'] / 365.25:.0f} years"
    if key == "category_tier" and detail.get("category"):
        return LABEL_BY_CATEGORY.get(detail["category"], signal.get("raw"))
    if key == "tld_abuse" and detail.get("tier"):
        # ".top (tier 1, rank 1)" is a table cell. In a sentence it is ".top".
        return str(signal.get("raw", "")).split(" (")[0]
    return signal.get("raw")


def _phrase(signal: dict, good: bool) -> str | None:
    entry = PLAIN.get(signal["key"])
    if not entry:
        return None
    # "The most expensive thing on sale is no price found" is not a sentence.
    # The absence of any price is its own finding and reads as one.
    if signal["key"] == "price_point" and not (signal.get("detail") or {}).get("highest"):
        return NO_PRICE
    # Likewise: an absent registration is a different finding from a recent one.
    if signal["key"] == "domain_age" and (signal.get("detail") or {}).get("registered") is False:
        return NOT_REGISTERED
    template = entry[0 if good else 1]
    try:
        return template.format(raw=_raw_for(signal))
    except (KeyError, IndexError):
        return template


def _helped_and_hurt(signals: list[dict]) -> tuple[list[str], list[str]]:
    """
    The lines worth reading, ranked by how much they actually moved the number.

    A signal that hurt is ranked by the points it cost — weight minus what it
    earned — so a heavy signal scoring badly outranks a light one scoring zero.
    A signal that helped is ranked by the points it brought in.
    """
    computed = [s for s in signals
                if s.get("status") == "ok" and s.get("normalized") is not None]

    hurt = sorted(
        (s for s in computed if s["normalized"] < BAD_UNDER),
        key=lambda s: s["weight"] - s["contribution"], reverse=True)
    helped = sorted(
        (s for s in computed if s["normalized"] >= GOOD_AT),
        key=lambda s: s["contribution"], reverse=True)

    def lines(rows, good):
        out = []
        for signal in rows:
            text = _phrase(signal, good)
            if text and text not in out:
                out.append(text)
            if len(out) >= MAX_POINTS:
                break
        return out

    return lines(helped, True), lines(hurt, False)


# ── What the business appears to be ─────────────────────────────────────────
def _company_name(crawl: dict, domain: str) -> str:
    """
    The name to call this merchant. Their own og:site_name if they published
    one, otherwise the first segment of the page title, which is where almost
    every site puts its name.
    """
    name = (crawl.get("site_name") or "").strip()
    if not name:
        title = (crawl.get("title") or "").strip()
        # "Acme Cloud — B2B workflow automation" → "Acme Cloud"
        for separator in ("|", "·", "—", "–", " - ", ":"):
            if separator in title:
                title = title.split(separator)[0]
                break
        name = title.strip()
    return (name or domain)[:80]


def _sentence(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


# Country names that take a definite article. GeoLite2 spells them without one,
# and "hosted in United States" is the kind of sentence that makes a reader stop
# trusting the rest of the paragraph.
_ARTICLE_COUNTRIES = (
    "United States", "United Kingdom", "United Arab Emirates", "Netherlands",
    "Philippines", "Czech Republic", "Russian Federation", "Dominican Republic",
    "Bahamas", "Maldives", "Gambia", "Isle of Man", "Republic of",
)


def _country(name: str) -> str:
    return f"the {name}" if name.startswith(_ARTICLE_COUNTRIES) else name


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _and_list(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def business(domain: str, evidence: dict, signals: list[dict]) -> dict:
    """
    A short paragraph on what this merchant appears to be.

    Built from the merchant's own words first — the page title, the meta
    description a site writes for search results and link previews, the about
    page — and then from what the crawl and the taxonomy actually observed. It
    describes; it never judges. The judging is the rest of the engine's job.
    """
    crawl = (evidence or {}).get("crawl") or {}
    by_key = _signal_map(signals)
    name = _company_name(crawl, domain)
    title = (crawl.get("title") or "").strip()
    description = (crawl.get("description") or "").strip()

    sentences: list[str] = []

    # 0. If nothing loaded, say that and stop. "Publishes no description of
    # itself" is true of a site that did not serve a page, and badly misleading:
    # the merchant may well have a description we never got to read.
    root = by_key.get("http_root", {})
    if root.get("status") == "ok" and (root.get("normalized") or 0) < 50:
        return {
            "name": name,
            "domain": domain,
            "title": title,
            "self_description": description,
            "paragraph": _sentence(
                f"Nothing could be read about {domain}: the site did not serve a usable page "
                f"on this run ({root.get('raw')}). There is no description here because there "
                f"was no storefront to describe, not because the merchant published none"),
            # Not "the site was reached": on a DNS failure it never was, and the
            # paragraph directly above this line says so. Claim only what is true
            # of every way a page can fail to arrive.
            "source": (
                "No page could be retrieved on this run. Nothing here is inferred from site "
                "content, because none was retrieved."
            ),
        }

    # 1. Who they say they are, in their words.
    own_words = description or title
    if description or len(title.split()) >= 4:
        sentences.append(_sentence(f"{name} ({domain}) describes itself as “{own_words[:220]}”"))
    elif own_words:
        # A one-word title is not a description of a business, and quoting it as
        # though it were reads as though we found more than we did.
        sentences.append(_sentence(
            f"{domain} publishes nothing about itself beyond the page title “{own_words}”"))
    else:
        sentences.append(_sentence(
            f"{domain} publishes no title or description of itself, so it has said nothing "
            f"about what it is"))

    # 2. What the site actually reads as.
    tier_signal = by_key.get("category_tier", {})
    detail = tier_signal.get("detail") or {}
    category = detail.get("category")
    if category:
        label = LABEL_BY_CATEGORY.get(category, category)
        tier = TIER_BY_CATEGORY.get(category, "standard")
        hits = (detail.get("hits") or [])[:3]
        evidence_phrase = f" on wording like “{_and_list(hits)}”" if hits else ""
        sentences.append(_sentence(
            f"Its own pages read as {label}{evidence_phrase} — "
            f"{TIER_PHRASE.get(tier, TIER_PHRASE['standard'])}"))
    elif tier_signal.get("status") == "ok":
        sentences.append(_sentence(
            "Its pages do not match any category on the acceptance list, so what it sells "
            "could not be pinned down from the text"))
    else:
        sentences.append(_sentence(
            "There was not enough text on the site to work out what it sells"))

    # 3. How it takes money.
    commerce = []
    price = by_key.get("price_point", {})
    if price.get("status") == "ok" and (price.get("detail") or {}).get("highest"):
        commerce.append(f"the highest price on the site is {price['raw']}")
    processor = by_key.get("processor", {})
    detected = (processor.get("detail") or {}).get("detected") or []
    if detected:
        commerce.append(f"checkout already runs through {_and_list(detected)}")
    recurring = by_key.get("recurring_billing", {})
    if (recurring.get("detail") or {}).get("recurring"):
        commerce.append("billing repeats on a subscription")
    elif recurring.get("status") == "ok" and commerce:
        # Only worth saying once there is some commerce to describe. "Purchases
        # look like one-off payments" on a ten-word holding page describes a
        # checkout that does not exist.
        commerce.append("purchases look like one-off payments rather than subscriptions")
    if commerce:
        sentences.append(_sentence(_and_list(commerce)))

    # 4. What it publishes.
    pages = sorted((crawl.get("pages") or {}).keys())
    fetched = crawl.get("links_fetched") or 0
    if pages:
        sentences.append(_sentence(
            f"Of the {_plural(fetched, 'page')} read on this run, it publishes "
            f"{_and_list(pages)} pages"))
    elif fetched == 1:
        sentences.append(_sentence(
            "The one page reached on this run was not a refund, terms, privacy, contact or "
            "pricing page"))
    elif fetched:
        sentences.append(_sentence(
            f"None of the {fetched} pages reached on this run were a refund, terms, "
            f"privacy, contact or pricing page"))

    # 5. How long it has existed, and where it sits.
    provenance = []
    age = by_key.get("domain_age", {})
    if age.get("status") == "ok" and (age.get("detail") or {}).get("days") is not None:
        days = age["detail"]["days"]
        provenance.append(
            f"the domain has been registered for {days / 365.25:.1f} years" if days >= 365
            else f"the domain was registered {days} days ago")
    geo = (evidence or {}).get("geoip") or {}
    if geo.get("country_name"):
        provenance.append(f"it is hosted in {_country(geo['country_name'])}")
    if provenance:
        sentences.append(_sentence(" and ".join(provenance)))

    return {
        "name": name,
        "domain": domain,
        "title": title,
        "self_description": description,
        "paragraph": " ".join(sentences),
        "source": (
            "Written from the merchant's own pages — the title and description they publish, "
            "and the pages this run actually fetched. No third-party data was used."
        ),
    }


# ── Why the engine decided what it decided ──────────────────────────────────
def why(result: dict, signals: list[dict]) -> dict:
    """
    The decision in plain English, for whoever has to act on it or explain it to
    the merchant. Same facts as `rationale`, none of the vocabulary.
    """
    band = result.get("band") or "manual_review"
    score = result.get("score")
    scored_band = result.get("scored_band")
    overrides = [o.get("code") for o in (result.get("overrides_applied") or [])]
    codes = [c["code"] if isinstance(c, dict) else c for c in (result.get("reason_codes") or [])]

    verdict = VERDICT.get(band, VERDICT["manual_review"])
    money = MONEY.get(band, MONEY["manual_review"])

    if score is None:
        score_line = (
            "None of the checks could be completed, so there is no number to act on. "
            "That is a failure on our side of the wire, not evidence against the merchant, "
            "which is why this is held rather than refused."
        )
    else:
        score_line = (
            f"The checks come to {score:g} out of 100. From 80 up is an automatic yes; 60 to 79 "
            f"is a yes with money held back; 40 to 59 goes to a person; under 40 is a no."
        )

    decisive = []
    for code in overrides:
        text = DECISIVE.get(code)
        if text:
            decisive.append(text)
        elif code:
            decisive.append(_sentence(REASON_CODES.get(code, code)))
    if decisive and scored_band and scored_band != band:
        decisive.insert(0, _sentence(
            f"On the number alone this would have been {BAND_PLAIN.get(scored_band, scored_band)}. "
            f"One finding overrules that"))

    helped, hurt = _helped_and_hurt(signals)

    confidence_pct = result.get("confidence_pct")
    if confidence_pct is None:
        confidence = ""
    elif confidence_pct >= 95:
        confidence = "Every check in the policy ran."
    else:
        confidence = (
            f"{confidence_pct}% of the checks could be completed this time. The rest could not "
            f"be reached, and were left out of the total rather than counted against the "
            f"merchant — a check that fails on our side is not evidence of anything."
        )

    next_step = NEXT_STEP.get(band, NEXT_STEP["manual_review"])

    paragraphs = [" ".join(p for p in [verdict, money] if p),
                  " ".join(p for p in [score_line] + decisive if p)]
    if hurt:
        paragraphs.append("What went against it: " + " ".join(hurt))
    if helped:
        paragraphs.append("What went in its favour: " + " ".join(helped))
    if confidence:
        paragraphs.append(confidence)
    paragraphs.append("What happens next: " + next_step)

    return {
        "verdict": verdict,
        "money": money,
        "score_line": score_line,
        "decisive": decisive,
        "helped": helped,
        "hurt": hurt,
        "confidence": confidence,
        "next_step": next_step,
        "paragraphs": paragraphs,
        "codes": codes,
    }


def summarise(domain: str, evidence: dict, signals: list[dict], result: dict) -> dict:
    """
    Both pieces of prose, ready to render. `written_by` says who wrote them, and
    stays "engine" unless a model rewrite is layered on afterwards.
    """
    return {
        "business": business(domain, evidence, signals),
        "why": why(result, signals),
        "written_by": "engine",
    }
