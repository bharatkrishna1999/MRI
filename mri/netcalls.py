"""
Every outbound call the engine makes, with a hard per-call timeout and a shared
deadline. Nothing in here raises: each helper returns a result dict carrying
either data or an `error`, so an upstream failure degrades one signal instead
of collapsing the run.
"""
from __future__ import annotations

import re
import socket
import ssl
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from .policy import PER_CALL_TIMEOUT_S
from .trace import Trace, fmt_bytes

UA = "MerchantRiskIntelligence/1.0 (+underwriting-bot)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_BODY_BYTES = 1_500_000


class Deadline:
    """
    A wall-clock budget shared by every call inside one evaluation.

    It also carries the run's Trace. The deadline is already threaded through
    every outbound call in the engine, so hanging the recorder off it means the
    network layer can narrate itself without a second parameter on twelve
    signatures — and without a thread-local, which would not survive the two
    thread pools the engine fans out across.
    """

    def __init__(self, seconds: float, trace: Trace | None = None):
        self.expires_at = time.monotonic() + seconds
        self.total = seconds
        self.trace = trace or Trace()

    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.05

    def budget(self, want: float = PER_CALL_TIMEOUT_S) -> float:
        return max(0.0, min(want, self.remaining()))


MAX_REDIRECTS = 4


def _client(timeout: float) -> httpx.Client:
    # Redirects are followed by hand in `fetch` rather than by httpx, because an
    # httpx timeout is per request attempt: with follow_redirects=True a 3 second
    # budget and a four hop chain is a 15 second call, and several of those in a
    # thread pool is how an 8 second evaluation takes 20. Following them here
    # means every hop is charged against the same shared deadline.
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
        follow_redirects=False,
        headers=HEADERS,
        verify=True,
    )


# ── HTML parsing ────────────────────────────────────────────────────────────
class _PageParser(HTMLParser):
    """Collects anchors and visible text. Stdlib only, no parser dependency."""

    _SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._text: list[str] = []
        self._skip_depth = 0
        self._in_a = False
        self._a_href = ""
        self._a_text: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        # <title> lives inside <head>, which is skipped for text extraction, so
        # it has to be flagged before the skip check or it is never captured.
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "a":
            self._in_a = True
            self._a_href = dict(attrs).get("href") or ""
            self._a_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._in_a:
            self.anchors.append((self._a_href, " ".join(self._a_text).strip()))
            self._in_a = False
            self._a_href = ""
            self._a_text = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if self._skip_depth:
            return
        stripped = data.strip()
        if not stripped:
            return
        self._text.append(stripped)
        if self._in_a:
            self._a_text.append(stripped)

    @property
    def text(self) -> str:
        return " ".join(self._text)


def parse_html(html: str) -> dict:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # malformed markup still yields whatever was parsed before the fault
    text = re.sub(r"\s+", " ", parser.text).strip()
    return {
        "text": text,
        "title": parser.title[:200],
        "anchors": parser.anchors,
        "word_count": len(text.split()),
    }


# ── HTTP ────────────────────────────────────────────────────────────────────
def fetch(url: str, deadline: Deadline, timeout: float = PER_CALL_TIMEOUT_S,
          purpose: str | None = None) -> dict:
    """
    GET a URL. Returns status, body, ttfb, final url, and a coarse `error_kind`
    that distinguishes 'the site is dead' from 'our fetch broke'.

    Pass `purpose` to have the call narrate itself into the run trace. Callers
    that already record a richer event of their own — the crawler names the page
    class it is after — leave it unset so the console shows one line per fetch.
    """
    started = time.monotonic()
    target = url
    trace = deadline.trace
    span = trace.event(
        "enrich" if purpose != "crawl" else "crawl",
        f"GET {url}", "running", purpose or "",
    ) if purpose else None

    def done(result: dict, status: str, detail: str) -> dict:
        if span:
            trace.event(span["phase"], span["label"], status, detail,
                        ref=span["id"], ms=int((time.monotonic() - started) * 1000))
        return result

    try:
        for _ in range(MAX_REDIRECTS + 1):
            # Re-budgeted every hop, so a redirect chain cannot outlive the
            # evaluation's deadline no matter how long it is.
            budget = deadline.budget(timeout)
            if budget <= 0.1:
                return done({"ok": False, "error": "deadline exhausted", "error_kind": "timeout"},
                            "error", "deadline exhausted before the request was sent")

            with _client(budget) as client:
                with client.stream("GET", target) as resp:
                    location = resp.headers.get("location")
                    if resp.is_redirect and location:
                        hop = str(resp.url.join(location))
                        if span:
                            trace.event(span["phase"], f"{resp.status_code} redirect",
                                        "info", f"{target} → {hop}")
                        target = hop
                        continue

                    ttfb_ms = int((time.monotonic() - started) * 1000)
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BODY_BYTES:
                            break
                    raw = b"".join(chunks)
                    encoding = resp.encoding or "utf-8"
                    body = raw.decode(encoding, errors="replace")
                    return done({
                        "ok": True,
                        "status": resp.status_code,
                        "url": str(resp.url),
                        "body": body,
                        "ttfb_ms": ttfb_ms,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "headers": {k.lower(): v for k, v in resp.headers.items()},
                    }, "ok" if resp.status_code < 400 else "warn",
                        f"HTTP {resp.status_code} · {fmt_bytes(size)} · ttfb {ttfb_ms} ms"
                        + (f" · {resp.headers.get('content-type', '').split(';')[0]}"
                           if resp.headers.get("content-type") else ""))
        return done({"ok": False, "error": "redirect loop", "error_kind": "http"},
                    "error", f"redirect loop after {MAX_REDIRECTS} hops")
    except httpx.ConnectTimeout:
        return done({"ok": False, "error": "connect timeout", "error_kind": "timeout"},
                    "error", "connect timeout")
    except httpx.ReadTimeout:
        return done({"ok": False, "error": "read timeout", "error_kind": "timeout"},
                    "error", "read timeout")
    except ssl.SSLCertVerificationError as exc:
        return done({"ok": False, "error": f"tls verify failed: {exc}", "error_kind": "tls"},
                    "error", f"TLS verification failed: {exc}")
    except httpx.ConnectError as exc:
        msg = str(exc).lower()
        if "name or service not known" in msg or "nodename nor servname" in msg \
                or "temporary failure in name resolution" in msg or "getaddrinfo" in msg:
            return done({"ok": False, "error": "dns resolution failed", "error_kind": "dns"},
                        "error", "DNS resolution failed")
        if "certificate" in msg or "ssl" in msg:
            return done({"ok": False, "error": f"tls error: {exc}", "error_kind": "tls"},
                        "error", f"TLS error: {exc}")
        return done({"ok": False, "error": f"connection refused: {exc}", "error_kind": "refused"},
                    "error", "connection refused")
    except Exception as exc:  # our problem, not the merchant's
        return done({"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_kind": "internal"},
                    "error", f"{type(exc).__name__}: {exc}")


def fetch_root(domain: str, deadline: Deadline) -> dict:
    """Try https://domain, then https://www.domain, then http://domain."""
    from .domains import root_urls

    last = None
    candidates = root_urls(domain)
    deadline.trace.event("enrich", "Resolving a reachable storefront root", "info",
                         " → ".join(candidates))
    for url in candidates:
        if deadline.expired():
            break
        result = fetch(url, deadline, purpose="storefront root")
        result["attempted"] = url
        if result.get("ok") and result.get("status", 0) < 400:
            return result
        last = result
        # A DNS failure on the apex will repeat on www; do not burn budget twice
        # unless the apex itself is what failed to resolve.
        if result.get("error_kind") == "dns" and url.startswith("https://www."):
            break
    return last or {"ok": False, "error": "no attempt made", "error_kind": "internal"}


# ── TLS ─────────────────────────────────────────────────────────────────────
def tls_info(domain: str, deadline: Deadline) -> dict:
    """
    Inspect the leaf certificate: validity, issuer, days to expiry.

    Done on a raw socket rather than through httpx so an invalid certificate is
    an observation we can score, not an exception that kills the fetch.
    """
    budget = deadline.budget(PER_CALL_TIMEOUT_S)
    if budget <= 0.1:
        return {"ok": False, "error": "deadline exhausted", "error_kind": "timeout"}

    ctx = ssl.create_default_context()
    trace = deadline.trace
    for host in (domain, f"www.{domain}"):
        started = time.monotonic()
        span = trace.event("enrich", f"TLS handshake {host}:443", "running",
                           "reading the leaf certificate on a raw socket")

        def finish(status: str, detail: str, result: dict) -> dict:
            trace.event("enrich", span["label"], status, detail, ref=span["id"],
                        ms=int((time.monotonic() - started) * 1000))
            return result

        try:
            with socket.create_connection((host, 443), timeout=budget) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
                    issuer = dict(x[0] for x in cert.get("issuer", ())).get(
                        "organizationName", "unknown"
                    )
                    not_after = cert.get("notAfter")
                    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                        tzinfo=timezone.utc
                    )
                    days = (expires - datetime.now(timezone.utc)).days
                    return finish(
                        "ok" if days > 0 else "warn",
                        f"valid · issuer {issuer} · {tls.version()} · expires "
                        f"{expires.date().isoformat()} ({days} days)",
                        {
                            "ok": True, "valid": True, "issuer": issuer,
                            "expires": expires.date().isoformat(), "days_to_expiry": days,
                            "protocol": tls.version(), "host": host,
                        })
        except ssl.SSLCertVerificationError as exc:
            reason = str(exc.verify_message or exc)[:120]
            return finish("warn", f"certificate did not verify: {reason}",
                          {"ok": True, "valid": False, "issuer": None,
                           "reason": reason, "host": host})
        except (socket.timeout, TimeoutError):
            return finish("error", "handshake timed out",
                          {"ok": False, "error": "tls handshake timeout",
                           "error_kind": "timeout"})
        except (socket.gaierror, ConnectionRefusedError, OSError) as exc:
            # try the www host before concluding there is no listener
            finish("warn", f"no listener on {host}:443 ({type(exc).__name__})", {})
            continue
        except Exception as exc:
            return finish("error", f"{type(exc).__name__}: {exc}",
                          {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                           "error_kind": "internal"})
    trace.event("enrich", "TLS inspection", "warn", "no TLS listener on port 443")
    return {"ok": True, "valid": False, "issuer": None,
            "reason": "no TLS listener on port 443", "host": domain}


# ── RDAP ────────────────────────────────────────────────────────────────────
RDAP_ENDPOINTS = ["https://rdap.org/domain/{d}", "https://www.rdap.net/domain/{d}"]


def rdap_lookup(domain: str, deadline: Deadline) -> dict:
    """Registration events and registrant entities. Free, keyless, authoritative."""
    trace = deadline.trace
    last_error = "no endpoint reached"
    for template in RDAP_ENDPOINTS:
        if deadline.expired():
            break
        url = template.format(d=domain)
        result = fetch(url, deadline, purpose="RDAP registration record")
        if not result.get("ok"):
            last_error = result.get("error", "unknown")
            continue
        if result["status"] == 404:
            trace.event("enrich", "RDAP", "warn",
                        f"{url} has no record for this domain — it is unregistered")
            return {"ok": True, "registered": False, "source": url}
        if result["status"] != 200:
            last_error = f"HTTP {result['status']}"
            continue
        try:
            import json

            data = json.loads(result["body"])
        except Exception:
            last_error = "malformed RDAP JSON"
            trace.event("enrich", "RDAP", "warn", f"{url} returned malformed JSON")
            continue
        parsed = _parse_rdap(data, url)
        trace.event("enrich", "RDAP parsed", "ok",
                    f"registered {parsed.get('created') or 'unknown'} · registrar "
                    f"{parsed.get('registrar') or 'not disclosed'}"
                    + (" · registrant behind a privacy proxy"
                       if parsed.get("privacy_proxy") else ""))
        return parsed
    trace.event("enrich", "RDAP", "error", f"no endpoint answered ({last_error})")
    return {"ok": False, "error": last_error}


def _parse_rdap(data: dict, source: str) -> dict:
    events = {}
    for event in data.get("events", []) or []:
        action = event.get("eventAction")
        date = event.get("eventDate")
        if action and date:
            events.setdefault(action, date)

    created = events.get("registration")
    expires = events.get("expiration")
    changed = events.get("last changed") or events.get("last update of RDAP database")

    registrar = ""
    privacy_hit = ""
    for entity in data.get("entities", []) or []:
        roles = entity.get("roles", []) or []
        name = _vcard_field(entity, "fn")
        org = _vcard_field(entity, "org")
        if "registrar" in roles and name:
            registrar = name
        if {"registrant", "administrative", "technical"} & set(roles):
            blob = f"{name} {org}".strip().lower()
            if blob and _looks_like_privacy_proxy(blob):
                privacy_hit = (name or org)[:80]

    # Some registries drop the registrant entity entirely rather than mask it.
    has_registrant = any(
        "registrant" in (e.get("roles") or []) for e in (data.get("entities") or [])
    )
    redacted_flag = any(
        "redacted" in str(r).lower() for r in (data.get("remarks") or [])
    ) or bool(data.get("redacted"))

    return {
        "ok": True,
        "registered": bool(created),
        "created": created,
        "expires": expires,
        "changed": changed,
        "registrar": registrar,
        "status": data.get("status", []),
        "privacy_proxy": bool(privacy_hit) or (not has_registrant) or redacted_flag,
        "privacy_evidence": privacy_hit or (
            "registrant entity absent or redacted" if not has_registrant or redacted_flag else ""
        ),
        "source": source,
    }


def _vcard_field(entity: dict, key: str) -> str:
    for item in (entity.get("vcardArray") or [[], []])[1]:
        try:
            if item[0] == key:
                value = item[3]
                return value if isinstance(value, str) else " ".join(map(str, value))
        except (IndexError, TypeError):
            continue
    return ""


PRIVACY_PROXY_MARKERS = [
    "privacy", "redacted", "whoisguard", "domains by proxy", "perfect privacy",
    "contact privacy", "withheld", "identity protect", "privacyprotect",
    "data protected", "not disclosed", "gdpr masked", "anonymize", "proxy protection",
    "domain protection services", "super privacy service",
]


def _looks_like_privacy_proxy(blob: str) -> bool:
    return any(marker in blob for marker in PRIVACY_PROXY_MARKERS)


# ── Link discovery ──────────────────────────────────────────────────────────
def internal_links(base_url: str, domain: str, anchors: list[tuple[str, str]]) -> list[dict]:
    """
    Resolve every anchor against the base URL and keep the ones that stay on the
    merchant's own registrable domain. Deduplicated, non-asset, http(s) only.
    """
    from .domains import domain_parts

    seen, out = set(), []
    for href, text in anchors:
        if not href:
            continue
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        try:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        host = parsed.netloc.split(":")[0].lower()
        parts = domain_parts(host)
        if f"{parts.domain}.{parts.suffix}" != domain:
            continue
        if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|pdf|zip|mp4|woff2?)$",
                     parsed.path, re.I):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/") or absolute
        if clean in seen:
            continue
        seen.add(clean)
        out.append({"url": clean, "text": (text or "")[:120], "href": href})
    return out


def emails_in(text: str) -> list[str]:
    found = re.findall(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text or "", re.I)
    return sorted({e.lower() for e in found})


# ── DNS ─────────────────────────────────────────────────────────────────────
def resolve_a(domain: str, deadline: Deadline) -> dict:
    """First A record for the domain. Used to place the merchant geographically."""
    budget = deadline.budget(PER_CALL_TIMEOUT_S)
    trace = deadline.trace
    if budget <= 0.1:
        return {"ok": False, "error": "deadline exhausted"}
    try:
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.lifetime = budget
        resolver.timeout = budget
        for name in (domain, f"www.{domain}"):
            with trace.step("enrich", f"DNS A {name}",
                            f"resolver {', '.join(resolver.nameservers[:2]) or 'system'}") as step:
                try:
                    answer = resolver.resolve(name, "A")
                    addresses = [r.address for r in answer]
                except Exception as exc:
                    step["status"], step["detail"] = "warn", f"{type(exc).__name__}"
                    continue
                if addresses:
                    step["detail"] = " ".join(addresses[:4]) + (
                        f" (+{len(addresses) - 4} more)" if len(addresses) > 4 else "")
                    return {"ok": True, "ip": addresses[0], "all": addresses, "name": name}
                step["status"], step["detail"] = "warn", "empty answer"
        return {"ok": True, "ip": None, "all": [], "error": "no A record"}
    except ImportError:
        with trace.step("enrich", f"DNS A {domain}", "stdlib resolver") as step:
            try:
                ip = socket.gethostbyname(domain)
            except Exception as exc:
                step["status"], step["detail"] = "error", f"{type(exc).__name__}: {exc}"
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            step["detail"] = ip
            return {"ok": True, "ip": ip, "all": [ip], "name": domain}
    except Exception as exc:
        trace.event("enrich", f"DNS A {domain}", "error", f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
