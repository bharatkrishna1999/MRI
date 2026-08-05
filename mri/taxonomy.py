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
    "crypto_tokens": ("Crypto and token sales", TIER_RESTRICTED, [
        "token sale", "presale", "ico", "initial coin offering", "airdrop",
        "connect wallet", "metamask", "tokenomics", "staking rewards", "apy",
        "buy $", "whitepaper", "defi", "nft mint", "web3",
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
    "lending": ("Lending", TIER_RESTRICTED, [
        "payday loan", "instant loan", "cash advance", "borrow up to",
        "loan approval in minutes", "apr", "no credit check loan", "installment loan",
        "lending platform",
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
RESTRICTED_KEYWORD_PATTERNS = [
    r"\btoken\s?sale\b", r"\bico\b", r"\bpresale\b", r"\bairdrop\b",
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

_CATEGORY_RE = {
    cid: [re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", re.I) for kw in kws]
    for cid, (_, _, kws) in CATEGORIES.items()
}


def infer_category(text: str) -> dict:
    """
    Infer the merchant's category from site text alone.

    Returns the best category, its tier, the hit count, and the runners-up so a
    reviewer can see how close the call was.
    """
    if not text or len(text.split()) < 20:
        return {"category": None, "tier": None, "score": 0, "hits": [], "ranked": []}

    scores = {}
    hits_by_cat = {}
    for cid, patterns in _CATEGORY_RE.items():
        hits = [p.pattern for p in patterns if p.search(text)]
        if hits:
            scores[cid] = len(hits)
            hits_by_cat[cid] = hits

    if not scores:
        return {"category": None, "tier": None, "score": 0, "hits": [], "ranked": []}

    # Restricted and elevated categories win ties: a site that reads as both a
    # SaaS tool and a token sale is underwritten as a token sale.
    def rank_key(item):
        cid, n = item
        return (n, TIER_ORDER.index(TIER_BY_CATEGORY[cid]))

    ranked = sorted(scores.items(), key=rank_key, reverse=True)
    best_id, best_n = ranked[0]
    return {
        "category": best_id,
        "tier": TIER_BY_CATEGORY[best_id],
        "score": best_n,
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
    found = []
    for rx in _RESTRICTED_RE:
        m = rx.search(text)
        if m:
            found.append(m.group(0).strip().lower())
    return sorted(set(found))
