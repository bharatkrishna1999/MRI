"""
Site liveness — 20 points.

Is there a real, serving, non-parked storefront behind this domain right now.

Note the distinction this module maintains: a site that answers with an error,
serves a parking page, or refuses the connection has been *measured* and scores
low. Only a failure on our side — our own exception, our own timeout budget
running out — returns unavailable.
"""
from __future__ import annotations

import re

from .base import ok, unavailable

# Strings that appear on the parking templates the large registrars and domain
# brokers serve. Matched against raw HTML, since much of this lives in scripts
# and meta tags rather than visible text.
PARKED_MARKERS = [
    "sedoparking", "sedo.com/search", "afternic", "buy this domain",
    "domain is for sale", "this domain is for sale", "parkingcrew", "bodis.com",
    "dan.com", "hugedomains", "domainmarket", "undeveloped.com",
    "namecheap parking", "godaddy.com/domainsearch", "this page is parked",
    "domain parking", "courtesy of the domain owner", "future home of something",
    "buydomains.com", "domain name is available for purchase",
    "the domain you are looking for is for sale", "inquire about this domain",
]
_PARKED_RE = re.compile("|".join(re.escape(m) for m in PARKED_MARKERS), re.I)

SHELL_WORD_THRESHOLD = 300


def http_root(bundle: dict) -> "Signal":
    kind = bundle.get("root_error_kind")
    status = bundle.get("root_status")

    if kind == "internal":
        return unavailable("http_root", f"Our fetcher failed before reaching the site ({bundle.get('root_error')}).")

    if kind == "dns":
        return ok("http_root", "DNS resolution failed", 0,
                  "The domain does not resolve to a host, so there is no storefront to underwrite.",
                  ["SITE_UNREACHABLE"])
    if kind == "refused":
        return ok("http_root", "connection refused", 5,
                  "The domain resolves but nothing is listening on the web ports, so there is no live storefront.",
                  ["SITE_UNREACHABLE"])
    if kind == "tls":
        return ok("http_root", "TLS handshake failed", 20,
                  "The site could not complete a TLS handshake, so a customer using a modern browser cannot reach checkout.",
                  ["SITE_HTTP_ERROR"])
    if kind == "timeout":
        return ok("http_root", "timed out", 25,
                  "The site did not respond within the 3 second per-call budget, which a paying customer would experience as a broken checkout.",
                  ["SITE_HTTP_ERROR"])

    if status is None:
        return unavailable("http_root", "No HTTP response was recorded for the root URL.")

    if 200 <= status < 300:
        return ok("http_root", f"HTTP {status}", 100,
                  f"The root URL serves a normal page (HTTP {status}).", url=bundle.get("root_url"))
    if 300 <= status < 400:
        return ok("http_root", f"HTTP {status}", 80,
                  f"The root URL redirects (HTTP {status}) rather than serving content directly.")
    if status in (401, 403):
        return ok("http_root", f"HTTP {status}", 35,
                  f"The root URL is gated behind authentication or a bot wall (HTTP {status}), so the public storefront cannot be inspected.",
                  ["SITE_HTTP_ERROR"])
    if 400 <= status < 500:
        return ok("http_root", f"HTTP {status}", 12,
                  f"The root URL returns a client error (HTTP {status}); there is no working homepage.",
                  ["SITE_HTTP_ERROR"])
    return ok("http_root", f"HTTP {status}", 8,
              f"The root URL returns a server error (HTTP {status}); the site is not serving customers.",
              ["SITE_HTTP_ERROR"])


def tls(info: dict) -> "Signal":
    if not info.get("ok"):
        return unavailable("tls", f"TLS inspection did not complete ({info.get('error')}).")

    if not info.get("valid"):
        reason_text = info.get("reason", "certificate failed validation")
        return ok("tls", f"invalid: {reason_text}", 0,
                  f"The TLS certificate does not validate ({reason_text}), which blocks card entry in every modern browser.",
                  ["TLS_INVALID"])

    days = info.get("days_to_expiry", 0)
    issuer = info.get("issuer") or "unknown issuer"
    if days < 0:
        return ok("tls", f"expired {abs(days)} days ago", 0,
                  f"The TLS certificate expired {abs(days)} days ago, so customers are being shown a browser security warning.",
                  ["TLS_INVALID"], issuer=issuer)
    if days < 15:
        return ok("tls", f"{days} days to expiry", 60,
                  f"The {issuer} certificate expires in {days} days; renewal has not happened yet.",
                  ["TLS_EXPIRING_SOON"], issuer=issuer)
    if days < 30:
        return ok("tls", f"{days} days to expiry", 85,
                  f"The {issuer} certificate is valid with {days} days remaining.", issuer=issuer)
    return ok("tls", f"valid, {days} days to expiry", 100,
              f"A valid {issuer} certificate is in place with {days} days remaining.",
              issuer=issuer, expires=info.get("expires"), protocol=info.get("protocol"))


def content_depth(bundle: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("content_depth", "No page was retrieved, so there is no content to measure.")

    words = bundle.get("word_count", 0)
    if words < 50:
        score, codes = 0, ["THIN_CONTENT"]
        reason = f"The homepage carries only {words} words, which is a placeholder rather than a storefront."
    elif words < 150:
        score, codes = 20, ["THIN_CONTENT"]
        reason = f"The homepage carries {words} words, far below the 300-word floor for a real storefront."
    elif words < SHELL_WORD_THRESHOLD:
        score, codes = 45, ["THIN_CONTENT"]
        reason = f"The homepage carries {words} words, under the 300-word threshold that separates a shell site from a working one."
    elif words < 800:
        score, codes = 80, []
        reason = f"The homepage carries {words} words, consistent with a real product site."
    else:
        score, codes = 100, []
        reason = f"The homepage carries {words} words of substantive content."

    return ok("content_depth", f"{words} words", score, reason, codes,
              word_count=words, threshold=SHELL_WORD_THRESHOLD)


def parked(bundle: dict) -> "Signal":
    if not bundle.get("root_ok"):
        return unavailable("parked", "No page was retrieved, so the parking check could not run.")

    html = bundle.get("root_html", "")
    match = _PARKED_RE.search(html)
    if match:
        return ok("parked", f"parking marker '{match.group(0)}'", 0,
                  f"The homepage serves a domain-parking template (matched '{match.group(0)}'), meaning nobody is running a business here.",
                  ["PARKED_DOMAIN"], marker=match.group(0))
    return ok("parked", "no parking markers", 100,
              "The homepage does not match any known domain-parking or for-sale template.")


def ttfb(bundle: dict) -> "Signal":
    value = bundle.get("ttfb_ms")
    if value is None:
        return unavailable("ttfb", "No response was timed, so time to first byte is unknown.")

    if value < 300:
        score, codes, note = 100, [], "fast"
    elif value < 800:
        score, codes, note = 85, [], "normal"
    elif value < 1500:
        score, codes, note = 65, [], "sluggish"
    elif value < 3000:
        score, codes, note = 40, ["SLOW_TTFB"], "slow"
    else:
        score, codes, note = 20, ["SLOW_TTFB"], "very slow"

    return ok("ttfb", f"{value} ms", score,
              f"The server returned its first byte in {value} ms ({note}), which is a proxy for whether real infrastructure sits behind the domain.",
              codes, ttfb_ms=value)
