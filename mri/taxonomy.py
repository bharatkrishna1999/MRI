"""
Acceptance taxonomy for a merchant of record selling digital goods.

Three tiers:
  standard   — underwrite normally
  elevated   — underwrite with reserve, these carry real chargeback exposure
  restricted — do not board

Category inference runs off site content only. The merchant's own dropdown
selection is never an input to a score; it is only ever compared against the
inferred category to produce a mismatch flag, because self-declaration is
exactly what a fraudulent applicant lies about.

Two rules keep the inference honest about *what the merchant sells*, as opposed
to what words happen to appear somewhere on the domain:

  * text that sits inside a prohibition is not evidence of the vertical. A
    merchant that publishes "we do not serve casinos" is telling us it is not a
    casino, and reading that sentence as gambling copy inverts its meaning.
  * a restricted tier is the only finding in the policy that declines an
    applicant on its own, so it has to clear an evidence bar before it is
    allowed to. A single incidental keyword routes to a human instead.
"""
from __future__ import annotations

import re

TIER_STANDARD = "standard"
TIER_ELEVATED = "elevated"
TIER_RESTRICTED = "restricted"

TIER_ORDER = [TIER_STANDARD, TIER_ELEVATED, TIER_RESTRICTED]

# Each category: id -> (label, tier, keyword patterns)
# Keywords are matched case-insensitively against visible page text.
CATEGORIES = {
    # ── Standard ────────────────────────────────────────────────────────────
    "saas_b2b": ("SaaS and B2B tools", TIER_STANDARD, [
        "saas", "software as a service", "b2b", "workspace", "dashboard",
        "team plan", "seats", "workflow automation", "crm", "analytics platform",
        "start free trial", "book a demo", "integrations",
    ]),
    "ai_apps_apis": ("AI apps and APIs", TIER_STANDARD, [
        "llm", "large language model", "prompt", "ai model", "inference api",
        "embeddings", "fine-tune", "fine tuning", "tokens per", "ai assistant",
        "generative ai", "api key",
    ]),
    "developer_tools": ("Developer tools", TIER_STANDARD, [
        "developer tool", "sdk", "cli", "open source", "npm install", "pip install",
        "docker", "ci/cd", "self-host", "api reference", "documentation", "github repo",
    ]),
    "digital_downloads": ("Digital downloads", TIER_STANDARD, [
        "instant download", "digital download", "downloadable", "template pack",
        "ui kit", "icon set", "preset", "lut pack", "ebook", "printable",
        "after purchase you will receive",
    ]),
    "online_courses": ("Online courses", TIER_STANDARD, [
        "online course", "curriculum", "lessons", "modules", "cohort",
        "enroll", "enrol", "video lessons", "lifetime access", "bootcamp",
        "certificate of completion",
    ]),
    "paid_newsletters": ("Paid newsletters", TIER_STANDARD, [
        "newsletter", "subscribe to the newsletter", "paid subscribers",
        "weekly issue", "back issues", "inbox every", "free and paid tiers",
    ]),
    "indie_games": ("Indie games", TIER_STANDARD, [
        "indie game", "steam", "itch.io", "gameplay", "playable demo",
        "wishlist on steam", "level editor", "soundtrack", "early access build",
    ]),
    "creative_services": ("Creative services", TIER_STANDARD, [
        "design studio", "branding", "portfolio", "our work", "case studies",
        "creative agency", "illustration", "video editing", "retainer",
        "project enquiry", "client work",
    ]),
    "mobile_apps": ("Mobile apps", TIER_STANDARD, [
        "app store", "google play", "download on the app store", "ios app",
        "android app", "testflight", "in-app purchase", "mobile app",
    ]),

    # ── Elevated ────────────────────────────────────────────────────────────
    "dropshipping": ("Dropshipping", TIER_ELEVATED, [
        "dropship", "drop shipping", "ships from our supplier",
        "delivery in 15-30 days", "delivery within 30 days", "aliexpress",
        "worldwide free shipping", "limited stock left", "order now while supplies",
    ]),
    "supplements": ("Supplements", TIER_ELEVATED, [
        "supplement", "nutraceutical", "capsules", "gummies", "whey protein",
        "fat burner", "testosterone booster", "detox", "not evaluated by the food and drug",
        "dietary supplement",
    ]),
    "trading_education": ("Trading education", TIER_ELEVATED, [
        "trading course", "forex signals", "trading signals", "prop firm",
        "funded account", "day trading", "candlestick", "scalping strategy",
        "trading mentorship", "profit guarantee",
    ]),
    "financial_advisory": ("Financial advisory", TIER_ELEVATED, [
        "financial advisory", "wealth management", "portfolio management",
        "investment advice", "registered investment adviser", "asset allocation",
        "retirement planning", "aum",
    ]),
    "ticketing": ("Ticketing", TIER_ELEVATED, [
        "buy tickets", "event tickets", "box office", "seating chart",
        "resale tickets", "ticket delivery", "e-ticket", "venue",
    ]),
    "marketplace_funds": ("Marketplaces holding third party funds", TIER_ELEVATED, [
        "seller payouts", "payout to sellers", "escrow", "wallet balance",
        "withdraw your earnings", "vendor dashboard", "commission on each sale",
        "connect your bank to receive payouts",
    ]),
    "subscription_boxes": ("Subscription boxes", TIER_ELEVATED, [
        "subscription box", "monthly box", "curated box", "ships monthly",
        "skip a month", "cancel anytime and keep", "box delivered every month",
    ]),

    # ── Restricted ──────────────────────────────────────────────────────────
    # "whitepaper", "apy" and "buy $" used to live here. They are ordinary B2B
    # and ecommerce vocabulary — every SaaS company publishes a whitepaper — and
    # a term that common cannot be allowed to contribute to a decline.
    "crypto_tokens": ("Crypto and token sales", TIER_RESTRICTED, [
        "token sale", "presale", "initial coin offering", "airdrop",
        "connect wallet", "metamask", "tokenomics", "staking rewards",
        "defi", "nft mint", "web3",
    ]),
    "gambling": ("Gambling", TIER_RESTRICTED, [
        "casino", "slots", "roulette", "sportsbook", "betting odds", "place a bet",
        "free spins", "wagering requirement", "jackpot", "poker room", "bookmaker",
    ]),
    "adult": ("Adult", TIER_RESTRICTED, [
        "adult content", "18+ only", "explicit content", "camgirl", "cam models",
        "xxx", "porn", "nsfw content subscription", "escort",
    ]),
    "cbd_vape": ("CBD and vape", TIER_RESTRICTED, [
        "cbd", "cannabidiol", "thc", "delta-8", "delta 8", "vape", "e-liquid",
        "nicotine pouches", "disposable vape", "hemp flower", "kratom",
    ]),
    "pharmacy": ("Pharmacy", TIER_RESTRICTED, [
        "online pharmacy", "prescription drugs", "no prescription needed",
        "generic viagra", "cialis", "buy medication online", "pharmacy without prescription",
        "semaglutide", "peptides for research",
    ]),
    "weapons": ("Weapons", TIER_RESTRICTED, [
        "firearms", "ammunition", "ar-15", "handgun", "silencer", "suppressor",
        "ballistic", "tactical knives", "body armor", "gun shop",
    ]),
    "debt_collection": ("Debt collection", TIER_RESTRICTED, [
        "debt collection", "collection agency", "recover outstanding debt",
        "charged-off accounts", "debt settlement", "credit repair",
        "remove negative items from your credit",
    ]),
    "mlm": ("MLM", TIER_RESTRICTED, [
        "multi-level marketing", "downline", "upline", "become a distributor",
        "recruit your team", "residual income", "join my team", "compensation plan",
        "passive income system", "financial freedom program",
    ]),
    # "apr" used to live here. It matches the month abbreviation in every
    # "Apr 2026" dateline on the web, which put a restricted tier on any site
    # with a blog index in the crawl.
    "lending": ("Lending", TIER_RESTRICTED, [
        "payday loan", "instant loan", "cash advance", "borrow up to",
        "loan approval in minutes", "annual percentage rate", "no credit check loan",
        "installment loan", "lending platform",
    ]),
}

CATEGORY_CHOICES = [
    {"id": cid, "label": label, "tier": tier}
    for cid, (label, tier, _) in CATEGORIES.items()
]

TIER_BY_CATEGORY = {cid: tier for cid, (_, tier, _) in CATEGORIES.items()}
LABEL_BY_CATEGORY = {cid: label for cid, (label, _, _) in CATEGORIES.items()}


# Restricted-vertical keyword scan. Deliberately narrower and higher-precision
# than the inference keywords above: these are terms that rarely appear on a
# site that is not actually in the vertical.
# `\bico\b` used to be on this list. "the ICO" is the UK Information
# Commissioner's Office, named in a large share of the privacy policies this
# crawler reads, so it flagged the merchants with the best data-protection
# hygiene. It is replaced by the unambiguous long forms.
RESTRICTED_KEYWORD_PATTERNS = [
    r"\btoken\s?sale\b", r"\binitial\s+coin\s+offering\b", r"\bico\s+(?:token|sale|launch)\b",
    r"\bpresale\b", r"\bairdrop\b",
    r"\bconnect\s+wallet\b", r"\btokenomics\b",
    r"\bcasino\b", r"\bsportsbook\b", r"\bfree\s+spins\b", r"\bwagering\s+requirement\b",
    r"\bno\s+prescription\b", r"\bgeneric\s+viagra\b",
    r"\bdelta[\s-]?8\b", r"\bhemp\s+flower\b", r"\bkratom\b",
    r"\bpayday\s+loan\b", r"\bno\s+credit\s+check\b",
    r"\bdownline\b", r"\bupline\b", r"\bcompensation\s+plan\b",
    r"\bescort\b", r"\bcamgirl\b",
    r"\bar[\s-]?15\b", r"\bsuppressor\b",
    r"\bdebt\s+settlement\b", r"\bcredit\s+repair\b",
]

_RESTRICTED_RE = [re.compile(p, re.I) for p in RESTRICTED_KEYWORD_PATTERNS]


# ── Prohibition context ─────────────────────────────────────────────────────
# A sentence that forbids a vertical is evidence the merchant is not in it. The
# clearest example is the acceptable-use page every payment company publishes:
# it names casinos, payday lenders, pharmacies and token sales in one paragraph,
# for the sole purpose of refusing them. Scanning that as product copy classifies
# a merchant as the exact set of businesses it will not do business with.
PROHIBITION_MARKERS = [
    r"prohibit", r"restricted business", r"not permitted", r"not allowed",
    r"unsupported business", r"unacceptable use", r"acceptable use",
    r"we do not (?:support|serve|accept|work with|allow|offer)",
    r"cannot be used for", r"may not be used (?:for|to)", r"forbidden",
    r"banned", r"disallowed", r"ineligible", r"excluded from",
    r"in violation of", r"illegal",
]
_PROHIBITION_RE = re.compile("|".join(PROHIBITION_MARKERS), re.I)

# Sentence-ish split. Legal pages are dense with semicolon-separated lists, and
# an item in a prohibited-business list belongs to the clause that introduced it,
# so semicolons and bullets do not end the prohibition's scope — full stops do.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# A restricted tier is the only inference in this policy that declines an
# applicant outright, so it has to be more than a single stray word.
RESTRICTED_MIN_HITS = 2


def strip_prohibited_context(text: str) -> str:
    """
    Drop the sentences in which the merchant is forbidding a vertical rather
    than selling it. Everything else is returned untouched.
    """
    if not text:
        return ""
    kept = [s for s in _SENTENCE_RE.split(text) if not _PROHIBITION_RE.search(s)]
    return " ".join(kept)


# Keyword kept alongside its pattern. A hit has to be reportable as the words
# that matched — "saas, b2b, dashboard" — because it is quoted back in the
# signal's reason, in the run trace and in the plain-English summary. The
# compiled source, which is what it used to report, is unreadable in all three.
_CATEGORY_RE = {
    cid: [(kw, re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", re.I)) for kw in kws]
    for cid, (_, _, kws) in CATEGORIES.items()
}


def infer_category(text: str) -> dict:
    """
    Infer the merchant's category from site text alone.

    Returns the best category, its tier, the hit count, and the runners-up so a
    reviewer can see how close the call was.

    `confident` is only ever False for a restricted result. It reports whether
    the restricted reading is strong enough to decline on, or whether it is a
    thin reading that a human should look at instead.
    """
    empty = {"category": None, "tier": None, "score": 0, "hits": [],
             "ranked": [], "confident": True, "runner_up": 0}

    text = strip_prohibited_context(text)
    if not text or len(text.split()) < 20:
        return empty

    scores = {}
    hits_by_cat = {}
    for cid, patterns in _CATEGORY_RE.items():
        hits = [kw for kw, rx in patterns if rx.search(text)]
        if hits:
            scores[cid] = len(hits)
            hits_by_cat[cid] = hits

    if not scores:
        return empty

    # Restricted and elevated categories win ties: a site that reads as both a
    # SaaS tool and a token sale is underwritten as a token sale.
    def rank_key(item):
        cid, n = item
        return (n, TIER_ORDER.index(TIER_BY_CATEGORY[cid]))

    ranked = sorted(scores.items(), key=rank_key, reverse=True)
    best_id, best_n = ranked[0]
    best_tier = TIER_BY_CATEGORY[best_id]

    # How strongly the site reads as something we would happily board. A
    # restricted call that merely ties with an ordinary reading of the same page
    # is not a decline, it is a question for a human.
    runner_up = max(
        (n for cid, n in ranked if TIER_BY_CATEGORY[cid] != TIER_RESTRICTED),
        default=0,
    )
    confident = best_tier != TIER_RESTRICTED or (
        best_n >= RESTRICTED_MIN_HITS and best_n > runner_up
    )

    return {
        "category": best_id,
        "tier": best_tier,
        "score": best_n,
        "confident": confident,
        "runner_up": runner_up,
        "hits": hits_by_cat[best_id][:6],
        "ranked": [
            {"category": cid, "label": LABEL_BY_CATEGORY[cid],
             "tier": TIER_BY_CATEGORY[cid], "hits": n}
            for cid, n in ranked[:5]
        ],
    }


def scan_restricted_keywords(text: str) -> list[str]:
    if not text:
        return []
    text = strip_prohibited_context(text)
    found = []
    for rx in _RESTRICTED_RE:
        m = rx.search(text)
        if m:
            found.append(m.group(0).strip().lower())
    return sorted(set(found))
