"""
Domain identity — 20 points.

What the registration record says about how long this merchant has existed and
how much they committed to the name. A domain bought last week for one year
under a privacy proxy on a high-abuse TLD is the standard shape of a bust-out.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..domains import tld_of
from .base import ok, unavailable

# Spamhaus "most abused top-level domains" — snapshot taken 2026-08-04.
# Tier 1 is the worst ten by abuse rate, tier 2 the next ten. Hardcoded on
# purpose: this list moves slowly and a live fetch would be a dependency that
# can fail during a demo.
TLD_ABUSE_TIER_1 = [
    "top", "cyou", "sbs", "cfd", "bond", "icu", "quest", "beauty", "rest", "autos",
]
TLD_ABUSE_TIER_2 = [
    "shop", "click", "buzz", "monster", "lol", "live", "work", "xyz", "life", "cc",
]
TLD_ABUSE_SNAPSHOT = "Spamhaus most-abused TLDs, snapshot 2026-08-04"


def _parse_rdap_date(value: str | None):
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def domain_age(rdap: dict) -> "Signal":
    if not rdap.get("ok"):
        return unavailable("domain_age", f"RDAP lookup did not return a record ({rdap.get('error')}).")
    if rdap.get("registered") is False:
        return ok("domain_age", "not registered", 0,
                  "RDAP reports no registration record for this domain at all.",
                  ["SITE_UNREACHABLE"])

    created = _parse_rdap_date(rdap.get("created"))
    if created is None:
        return unavailable("domain_age", "RDAP returned no registration event for this domain.")

    days = (datetime.now(timezone.utc) - created).days
    if days < 30:
        score, codes = 0, ["DOM_AGE_LT_30", "DOM_AGE_LT_180"]
    elif days < 90:
        score, codes = 18, ["DOM_AGE_LT_180"]
    elif days < 180:
        score, codes = 38, ["DOM_AGE_LT_180"]
    elif days < 365:
        score, codes = 60, []
    elif days < 730:
        score, codes = 78, []
    elif days < 1095:
        score, codes = 90, []
    else:
        score, codes = 100, []

    years = days / 365.25
    if days < 30:
        reason = f"Domain was created {days} days ago, inside the 30-day window where bust-out fraud clusters."
    elif days < 365:
        reason = f"Domain is {days} days old, young enough that there is no trading history to underwrite against."
    else:
        reason = f"Domain has been registered for {years:.1f} years, long past the window where fraud domains are rotated."

    return ok("domain_age", f"{days} days", score, reason, codes,
              created=created.date().isoformat(), days=days, registrar=rdap.get("registrar", ""))


def registration_term(rdap: dict) -> "Signal":
    if not rdap.get("ok"):
        return unavailable("registration_term", f"RDAP lookup did not return a record ({rdap.get('error')}).")
    created = _parse_rdap_date(rdap.get("created"))
    expires = _parse_rdap_date(rdap.get("expires"))
    if created is None or expires is None:
        return unavailable("registration_term", "RDAP did not publish both a creation and an expiry date.")

    years = (expires - created).days / 365.25
    if years < 1.2:
        score, codes = 30, ["DOM_TERM_MINIMUM"]
        reason = "Domain is registered for the one-year minimum, the cheapest possible commitment to the name."
    elif years < 2.2:
        score, codes = 62, []
        reason = f"Domain is registered {years:.0f} years out, a normal renewal cycle for a working business."
    elif years < 5:
        score, codes = 85, []
        reason = f"Domain is paid up {years:.0f} years ahead, which fraud operations rarely bother to do."
    else:
        score, codes = 100, []
        reason = f"Domain is paid up {years:.0f} years ahead, a long-horizon commitment to the brand."

    return ok("registration_term", f"{years:.1f} years", score, reason, codes,
              expires=expires.date().isoformat())


def privacy_proxy(rdap: dict) -> "Signal":
    if not rdap.get("ok"):
        return unavailable("privacy_proxy", f"RDAP lookup did not return a record ({rdap.get('error')}).")
    if rdap.get("registered") is False:
        return unavailable("privacy_proxy", "No registration record to inspect for registrant details.")

    masked = bool(rdap.get("privacy_proxy"))
    if masked:
        evidence = rdap.get("privacy_evidence") or "registrant details withheld"
        return ok("privacy_proxy", "present", 35,
                  f"Registrant identity is hidden behind a privacy service ({evidence}), so the name on the domain cannot be checked against the applicant.",
                  ["DOM_PRIVACY_PROXY"], evidence=evidence)
    return ok("privacy_proxy", "absent", 100,
              "Registrant details are published in RDAP, so the domain owner can be checked against the applicant.")


def tld_abuse(domain: str) -> "Signal":
    suffix = tld_of(domain)
    last = suffix.rsplit(".", 1)[-1] if suffix else ""
    if last in TLD_ABUSE_TIER_1:
        rank = TLD_ABUSE_TIER_1.index(last) + 1
        return ok("tld_abuse", f".{last} (tier 1, rank {rank})", 15,
                  f".{last} sits at rank {rank} on the most-abused TLD list, where the majority of registrations are used for spam or fraud.",
                  ["TLD_HIGH_ABUSE"], tier=1, rank=rank, source=TLD_ABUSE_SNAPSHOT)
    if last in TLD_ABUSE_TIER_2:
        rank = TLD_ABUSE_TIER_2.index(last) + 11
        return ok("tld_abuse", f".{last} (tier 2, rank {rank})", 50,
                  f".{last} sits at rank {rank} on the most-abused TLD list, elevated but with substantial legitimate use.",
                  ["TLD_ELEVATED_ABUSE"], tier=2, rank=rank, source=TLD_ABUSE_SNAPSHOT)
    return ok("tld_abuse", f".{suffix}", 100,
              f".{suffix} does not appear on the most-abused TLD list.",
              tier=0, source=TLD_ABUSE_SNAPSHOT)
