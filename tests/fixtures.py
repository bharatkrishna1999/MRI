"""
Synthetic merchant sites used to exercise the whole pipeline offline.

These are not stubs of the scoring functions — they are HTML documents fed into
the real parser, the real link discovery and the real signal code, so a test
failure means the engine changed behaviour, not that a mock drifted.
"""
from __future__ import annotations

FILLER = ("We build tools for engineering teams that need to ship faster. " * 40)
POLICY_FILLER = ("This agreement sets out the terms under which the service is provided. " * 30)


def _page(title: str, body: str, description: str = "", site_name: str = "") -> str:
    head = f"<title>{title}</title>"
    if description:
        head += f'<meta name="description" content="{description}">'
    if site_name:
        head += f'<meta property="og:site_name" content="{site_name}">'
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


NAV = """
<nav>
  <a href="/pricing">Pricing</a>
  <a href="/docs">Docs</a>
  <a href="/contact">Contact us</a>
  <a href="/legal/terms">Terms of Service</a>
  <a href="/legal/privacy">Privacy Policy</a>
  <a href="/legal/refunds">Refund Policy</a>
  <a href="/blog">Blog</a>
  <a href="https://twitter.com/acme">Twitter</a>
</nav>
"""

# ── A clean SaaS merchant: every policy page, Stripe checkout, standard tier ──
GOOD_SITE = {
    "https://goodsaas.com/": _page("Acme Cloud — B2B SaaS workflow automation", f"""
      {NAV}
      <h1>Acme Cloud</h1>
      <p>Acme Cloud Inc is a SaaS platform for B2B teams. Start free trial or book a demo.
      Our dashboard gives your team seats, integrations and workflow automation.</p>
      <p>Pricing from $29 per month, billed monthly. Cancel anytime.</p>
      <p>{FILLER}</p>
      <script src="https://js.stripe.com/v3/"></script>
      <footer>© 2019 Acme Cloud Inc · support@goodsaas.com</footer>""",
      description="Acme Cloud is workflow automation for B2B engineering teams. "
                  "Plans from $29 a month, cancel anytime.",
      site_name="Acme Cloud"),
    "https://goodsaas.com/pricing": _page("Pricing — Acme Cloud", f"""
      <h1>Plans</h1><p>Starter $29 per month. Team $99 per month. Enterprise $499 per month.
      All plans billed monthly, cancel anytime.</p><p>{FILLER}</p>"""),
    "https://goodsaas.com/legal/terms": _page("Terms of Service", f"<h1>Terms of Service</h1><p>{POLICY_FILLER}</p>"),
    "https://goodsaas.com/legal/privacy": _page("Privacy Policy", f"<h1>Privacy Policy</h1><p>{POLICY_FILLER}</p>"),
    "https://goodsaas.com/legal/refunds": _page("Refund Policy", f"""
      <h1>Refund Policy</h1><p>You may cancel your subscription at any time and request a
      full refund within 30 days.</p><p>{POLICY_FILLER}</p>"""),
    "https://goodsaas.com/contact": _page("Contact", """
      <h1>Contact us</h1>
      <p>Email support@goodsaas.com and a human replies within one business day.
      Our support desk is staffed Monday to Friday. Enterprise customers can reach
      their named account manager directly. Postal address: 100 Market Street,
      San Francisco, California. For billing questions use billing@goodsaas.com.</p>"""),
    "https://goodsaas.com/docs": _page("Docs", f"<h1>API reference</h1><p>{FILLER}</p>"),
    "https://goodsaas.com/blog": _page("Blog", f"<h1>Blog</h1><p>{FILLER}</p>"),
}

# ── A shell: thin, no policy pages, no processor ─────────────────────────────
SHELL_SITE = {
    "https://shellco.top/": _page("Welcome", """
      <h1>Welcome to our store</h1>
      <p>Best deals online. Order now while supplies last.</p>
      <a href="/shop">Shop</a>"""),
    "https://shellco.top/shop": _page("Shop", "<h1>Shop</h1><p>Coming soon.</p>"),
}

# ── A parked domain ──────────────────────────────────────────────────────────
PARKED_SITE = {
    "https://parkedthing.com/": _page("parkedthing.com", """
      <h1>parkedthing.com</h1>
      <p>This domain is for sale. Inquire about this domain.</p>
      <script src="https://sedoparking.com/park.js"></script>"""),
}

# ── A restricted merchant: reads as a token sale ─────────────────────────────
RESTRICTED_SITE = {
    "https://tokenlaunch.xyz/": _page("TokenLaunch — presale live", f"""
      {NAV}
      <h1>TokenLaunch presale is live</h1>
      <p>Connect wallet with MetaMask to join the token sale. Read our whitepaper for
      tokenomics and staking rewards APY. This DeFi web3 airdrop is limited.</p>
      <p>{FILLER}</p>"""),
    "https://tokenlaunch.xyz/legal/terms": _page("Terms", f"<h1>Terms</h1><p>{POLICY_FILLER}</p>"),
    "https://tokenlaunch.xyz/legal/privacy": _page("Privacy", f"<h1>Privacy</h1><p>{POLICY_FILLER}</p>"),
    "https://tokenlaunch.xyz/legal/refunds": _page("Refunds", f"<h1>Refunds</h1><p>{POLICY_FILLER}</p>"),
    "https://tokenlaunch.xyz/contact": _page("Contact", "<h1>Contact</h1><p>hi@tokenlaunch.xyz</p>"),
    "https://tokenlaunch.xyz/pricing": _page("Pricing", f"<h1>Pricing</h1><p>1 ETH minimum.</p><p>{FILLER}</p>"),
    "https://tokenlaunch.xyz/docs": _page("Docs", f"<p>{FILLER}</p>"),
    "https://tokenlaunch.xyz/blog": _page("Blog", f"<p>{FILLER}</p>"),
}

# ── A payments platform: clean merchant, blocklist in its own acceptable-use ──
# The shape that produced a false decline in production. Everything a payment
# company, marketplace or compliance vendor publishes about the businesses it
# *refuses* lives on its own domain, one link from the homepage, and reads to a
# keyword scanner exactly like a merchant in all of those verticals at once.
PAYMENTS_PLATFORM_SITE = {
    "https://payflow.com/": _page("Payflow | Financial Infrastructure for the Internet", f"""
      {NAV}
      <a href="/legal/restricted-businesses">Restricted businesses</a>
      <h1>Payflow</h1>
      <p>Millions of companies use Payflow to accept payments online. Our B2B SaaS
      dashboard, integrations and workflow automation let your team ship faster.
      Book a demo or start free trial. Read the API reference and install the SDK.</p>
      <p>We serve fintech, web3 and marketplace businesses. Pricing is $29 per month,
      billed monthly, cancel anytime. Updated Apr 2026.</p>
      <p>{FILLER}</p>
      <footer>© 2011 Payflow Inc · support@payflow.com</footer>"""),
    "https://payflow.com/legal/restricted-businesses": _page("Restricted businesses — Payflow", """
      <h1>Restricted businesses</h1>
      <p>The following categories are prohibited from using Payflow. Businesses in
      violation of this policy will be offboarded.</p>
      <ul>
        <li>Casino, sportsbook and other gambling activity, including free spins
            promotions and any wagering requirement.</li>
        <li>Adult content, escort services and camgirl platforms.</li>
        <li>Payday loan, cash advance and no credit check lending.</li>
        <li>Credit repair and debt settlement services.</li>
        <li>Multi-level marketing, downline recruitment and compensation plan schemes.</li>
        <li>Pharmacy with no prescription, generic viagra and unlicensed medication.</li>
        <li>Kratom, hemp flower, delta-8 and vape products.</li>
        <li>Firearms, ammunition, AR-15 parts and suppressor sales.</li>
        <li>Token sale, initial coin offering, presale and airdrop promotions.</li>
      </ul>"""),
    "https://payflow.com/pricing": _page("Pricing — Payflow", f"""
      <h1>Plans</h1><p>Starter $29 per month. Team $99 per month.
      All plans billed monthly, cancel anytime.</p><p>{FILLER}</p>"""),
    "https://payflow.com/legal/terms": _page("Terms of Service", f"<h1>Terms of Service</h1><p>{POLICY_FILLER}</p>"),
    "https://payflow.com/legal/privacy": _page("Privacy Policy", f"""
      <h1>Privacy Policy</h1>
      <p>You may complain to the ICO if you are unhappy with how we handle your data.</p>
      <p>{POLICY_FILLER}</p>"""),
    "https://payflow.com/legal/refunds": _page("Refund Policy", f"""
      <h1>Refund Policy</h1><p>You may cancel your subscription at any time and request a
      full refund within 30 days.</p><p>{POLICY_FILLER}</p>"""),
    "https://payflow.com/contact": _page("Contact", """
      <h1>Contact us</h1><p>Email support@payflow.com and a human replies within one
      business day. Our support desk is staffed Monday to Friday. Postal address:
      100 Market Street, San Francisco, California.</p>"""),
    "https://payflow.com/docs": _page("Docs", f"<h1>API reference</h1><p>{FILLER}</p>"),
    "https://payflow.com/blog": _page("Blog", f"<h1>Blog</h1><p>{FILLER}</p>"),
}

# ── The shape that produced the false hold this policy version exists to fix ──
# A live page on a clean TLD with a valid certificate, a fast first byte and no
# parking template — and nothing else. No refund page, no terms, no privacy, no
# contact, no prices, no processor, and 125 words that never say what is being
# sold. Under v1.1 the freebies averaged the emptiness away and it scored 47.2,
# a hold. It is not a marginal merchant; there is no merchant.
BROCHURE_SITE = {
    "https://brochure.in/": _page("Cogreen", """
      <h1>Hiking the mountains</h1>
      <p>We are hiking the mountains of Assam and Darjeeling. Our guided treks take small
      groups through tea gardens, cloud forest and ridge lines above the valley. Walk the old
      bridle paths and watch the sun come up over the range. In the lower hills we run shorter
      walks, staying in village homes and small guest houses along the way. Every trek is led
      by guides who grew up on these trails and know the weather, the river crossings and the
      best places to stop for tea. Groups are kept small. Seasons run from October through
      April when the skies are clear and the ridges are open. Come walk with us.</p>
      <a href="/shop">Shop</a><a href="/gallery">Gallery</a>""",
      description="We are Hiking the Mountains of Assam and Darjeeling"),
    "https://brochure.in/gallery": _page("Gallery", "<h1>Gallery</h1><p>Photographs from the trail.</p>"),
}

# ── Thin on words, but unmistakably a shop ───────────────────────────────────
# The counter-case to the one above, and the reason the fix is a conjunction
# rather than a word count. A one-page store says very little and still sells:
# real prices, a real processor, a real refund policy. It must not be swept up.
SMALL_SHOP_SITE = {
    "https://tinyshop.com/": _page("Ridgeline Prints — hand-pulled screen prints", """
      <a href="/refunds">Refunds</a><a href="/terms">Terms</a>
      <a href="/privacy">Privacy</a><a href="/contact">Contact</a>
      <h1>Ridgeline Prints</h1>
      <p>Hand-pulled screen prints, made to order. Prints are $45. Framed prints are $90.
      Add to cart and we ship within a week.</p>
      <script src="https://js.stripe.com/v3/"></script>
      <footer>© 2016 Ridgeline Prints · hello@tinyshop.com</footer>""",
      description="Hand-pulled screen prints, made to order, from $45."),
    "https://tinyshop.com/refunds": _page("Refunds", f"""
      <h1>Refunds</h1><p>Return any print within 30 days for a full refund.</p>
      <p>{POLICY_FILLER}</p>"""),
    "https://tinyshop.com/terms": _page("Terms", f"<h1>Terms</h1><p>{POLICY_FILLER}</p>"),
    "https://tinyshop.com/privacy": _page("Privacy", f"<h1>Privacy</h1><p>{POLICY_FILLER}</p>"),
    "https://tinyshop.com/contact": _page("Contact", """
      <h1>Contact</h1><p>Email hello@tinyshop.com and we reply within a day.
      Studio address: 4 Mill Lane, Hebden Bridge.</p>"""),
}

# ── A real business in a vertical the taxonomy simply does not carry ─────────
# Writes at length, publishes every policy page, takes payments — and still
# matches no category, because guided trekking is not on the acceptance ladder.
# That is a gap in our taxonomy, not a finding against the merchant, and it must
# keep scoring as one.
UNLISTED_VERTICAL_SITE = {
    "https://trailco.com/": _page("Trailco — guided ridge walks", f"""
      {NAV}
      <h1>Trailco</h1>
      <p>Trailco has run guided ridge walks since 2009. Book a demo of the route planner or
      browse departures. Walks from $450 per person.</p>
      <p>{FILLER}</p>
      <script src="https://js.stripe.com/v3/"></script>
      <footer>© 2009 Trailco Ltd · walk@trailco.com</footer>"""),
    "https://trailco.com/pricing": _page("Departures", f"""
      <h1>Departures</h1><p>Four-day ridge walk $450. Eight-day traverse $890.</p>
      <p>{FILLER}</p>"""),
    "https://trailco.com/legal/terms": _page("Terms", f"<h1>Terms</h1><p>{POLICY_FILLER}</p>"),
    "https://trailco.com/legal/privacy": _page("Privacy", f"<h1>Privacy</h1><p>{POLICY_FILLER}</p>"),
    "https://trailco.com/legal/refunds": _page("Refunds", f"""
      <h1>Refunds</h1><p>Cancel up to 30 days before departure for a full refund.</p>
      <p>{POLICY_FILLER}</p>"""),
    "https://trailco.com/contact": _page("Contact", """
      <h1>Contact</h1><p>Email walk@trailco.com. Office: 2 Fell Road, Keswick.
      Our team answers within one business day.</p>"""),
    "https://trailco.com/docs": _page("Route notes", f"<h1>Route notes</h1><p>{FILLER}</p>"),
    "https://trailco.com/blog": _page("Journal", f"<h1>Journal</h1><p>{FILLER}</p>"),
}

# ── A real merchant our fetcher cannot render ────────────────────────────────
# Client-rendered application: the static HTML is a shell, and every word of
# copy, every nav link and every policy link is injected by JavaScript after
# load. We do not run JavaScript, so this parses to zero words and zero anchors.
# Every absence the policy looks for is then guaranteed rather than observed,
# which is why the empty-storefront finding must not fire on it.
SPA_SHELL_SITE = {
    "https://reactco.com/": (
        '<!doctype html><html><head><title>Nimbus</title>'
        '<link rel="stylesheet" href="/assets/index-4a2f.css">'
        '</head><body><div id="root"></div>'
        '<script type="module" src="/assets/index-9c1b.js"></script>'
        '</body></html>'),
}

SITES = {
    "goodsaas.com": GOOD_SITE,
    "reactco.com": SPA_SHELL_SITE,
    "brochure.in": BROCHURE_SITE,
    "tinyshop.com": SMALL_SHOP_SITE,
    "trailco.com": UNLISTED_VERTICAL_SITE,
    "shellco.top": SHELL_SITE,
    "parkedthing.com": PARKED_SITE,
    "tokenlaunch.xyz": RESTRICTED_SITE,
    "payflow.com": PAYMENTS_PLATFORM_SITE,
}


def days_ago(days: int) -> str:
    """
    RDAP timestamps for fixtures, relative to now.

    Domain age is scored against the clock, so a hardcoded creation date is a
    test that passes until it silently does not: the shell fixture was written
    with a date inside the 30-day bust-out window and started failing the day it
    aged out of it.
    """
    import datetime

    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")


def days_ahead(days: int) -> str:
    import datetime

    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")


def rdap(created: str, expires: str, privacy: bool = False, registrar: str = "Example Registrar") -> dict:
    return {
        "ok": True, "registered": True, "created": created, "expires": expires,
        "registrar": registrar, "privacy_proxy": privacy,
        "privacy_evidence": "Privacy Service" if privacy else "",
        "status": ["client transfer prohibited"], "source": "test",
    }


def tls(days: int = 200, issuer: str = "Let's Encrypt", valid: bool = True) -> dict:
    if not valid:
        return {"ok": True, "valid": False, "issuer": None, "reason": "hostname mismatch"}
    return {"ok": True, "valid": True, "issuer": issuer, "expires": "2027-01-01",
            "days_to_expiry": days, "protocol": "TLSv1.3", "host": "example.com"}
