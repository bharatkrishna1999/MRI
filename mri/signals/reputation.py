"""
Reputation — 5 points.

Google Safe Browsing v4 is the primary source. It is free and its quota is
generous, but it does require an API key, so the engine also carries a keyless
fallback: a DNS query against the Spamhaus Domain Block List. Neither costs
money. If neither can answer, the signal returns unavailable and drops out of
the denominator rather than handing the merchant free points or a false decline.
"""
from __future__ import annotations

import json
import os

from ..netcalls import Deadline, HEADERS
from ..policy import PER_CALL_TIMEOUT_S
from .base import ok, unavailable

SAFE_BROWSING_URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
THREAT_TYPES = ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE",
                "POTENTIALLY_HARMFUL_APPLICATION"]

# Spamhaus DBL return codes. 127.0.1.x is a listing; 127.255.255.x is an error
# (usually "query refused, you are over the free-use limit").
DBL_LISTING_PREFIX = "127.0.1."
DBL_ERROR_PREFIX = "127.255.255."


def api_key() -> str:
    return (os.environ.get("SAFE_BROWSING_API_KEY") or "").strip()


def _safe_browsing(domain: str, deadline: Deadline) -> dict:
    key = api_key()
    if not key:
        return {"ok": False, "error": "no SAFE_BROWSING_API_KEY configured"}

    budget = deadline.budget(PER_CALL_TIMEOUT_S)
    if budget <= 0.1:
        return {"ok": False, "error": "deadline exhausted"}

    payload = {
        "client": {"clientId": "merchant-risk-intelligence", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": THREAT_TYPES,
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [
                {"url": f"http://{domain}/"},
                {"url": f"https://{domain}/"},
                {"url": f"https://www.{domain}/"},
            ],
        },
    }

    try:
        import httpx

        with httpx.Client(timeout=budget, headers=HEADERS) as client:
            response = client.post(SAFE_BROWSING_URL, params={"key": key}, json=payload)
        if response.status_code != 200:
            return {"ok": False, "error": f"Safe Browsing HTTP {response.status_code}"}
        matches = response.json().get("matches", [])
        return {"ok": True, "source": "Google Safe Browsing v4",
                "matches": matches, "listed": bool(matches)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _spamhaus_dbl(domain: str, deadline: Deadline) -> dict:
    budget = deadline.budget(PER_CALL_TIMEOUT_S)
    if budget <= 0.1:
        return {"ok": False, "error": "deadline exhausted"}
    try:
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.lifetime = budget
        resolver.timeout = budget
        answers = [r.address for r in resolver.resolve(f"{domain}.dbl.spamhaus.org", "A")]
    except Exception as exc:
        name = type(exc).__name__
        if name in ("NXDOMAIN", "NoAnswer"):
            return {"ok": True, "source": "Spamhaus DBL", "listed": False, "codes": []}
        return {"ok": False, "error": f"{name}: {exc}"}

    if any(a.startswith(DBL_ERROR_PREFIX) for a in answers):
        return {"ok": False, "error": f"Spamhaus DBL refused the query ({answers[0]})"}
    listed = [a for a in answers if a.startswith(DBL_LISTING_PREFIX)]
    return {"ok": True, "source": "Spamhaus DBL", "listed": bool(listed), "codes": listed}


def lookup(domain: str, deadline: Deadline) -> dict:
    result = _safe_browsing(domain, deadline)
    if result.get("ok"):
        return result
    primary_error = result.get("error", "unknown")
    fallback = _spamhaus_dbl(domain, deadline)
    if fallback.get("ok"):
        fallback["primary_error"] = primary_error
        return fallback
    return {"ok": False, "error": primary_error, "fallback_error": fallback.get("error")}


def safe_browsing(result: dict) -> "Signal":
    if not result.get("ok"):
        detail = result.get("error", "unknown")
        if result.get("fallback_error"):
            detail = f"{detail}; fallback: {result['fallback_error']}"
        return unavailable("safe_browsing", f"No threat-intelligence source could be reached ({detail}).")

    source = result.get("source", "threat intelligence")

    if result.get("listed"):
        if result.get("matches"):
            threats = sorted({m.get("threatType", "THREAT") for m in result["matches"]})
            detail = ", ".join(t.lower().replace("_", " ") for t in threats)
        else:
            detail = "domain block list"
        return ok("safe_browsing", f"listed by {source}", 0,
                  f"{source} lists this domain ({detail}), which is a direct hit on a live threat feed.",
                  ["SAFEBROWSING_HIT"], source=source,
                  matches=result.get("matches", []),
                  listing_codes=result.get("codes", []))

    return ok("safe_browsing", f"clean per {source}", 100,
              f"{source} has no listing for this domain.", source=source)
