"""
Evaluation engine.

One domain in, one decision out, inside 8 seconds.

Failure behaviour is the part worth reading. Every signal either computes a
value or reports itself unavailable. Unavailable signals are removed from the
weighted denominator, so the score is always the merchant's average over the
evidence that actually arrived, and confidence carries the fraction of policy
weight that could be computed. Nothing ever falls back to zero, because a zero
is indistinguishable from a real failing signal and would produce a false
decline out of an upstream outage.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from . import geo as geoip
from .crawl import crawl_site
from .domains import InvalidDomain, normalize_domain
from .narrative import summarise
from .netcalls import Deadline, fetch_root, rdap_lookup, resolve_a, tls_info
from .policy import (
    CATEGORY_DESCRIPTIONS,
    CATEGORY_LABELS,
    CATEGORY_WEIGHTS,
    CONFIDENCE_FLOOR,
    GLOBAL_TIMEOUT_S,
    POLICY_VERSION,
    REASON_CODES,
    SIGNAL_SPEC,
    TOTAL_WEIGHT,
)
from .signals import category as sig_category
from .signals import commercial as sig_commercial
from .signals import consistency as sig_consistency
from .signals import identity as sig_identity
from .signals import liveness as sig_liveness
from .signals import payment as sig_payment
from .signals import reputation as sig_reputation
from .signals.base import OK, Signal, unavailable
from .taxonomy import infer_category
from .trace import Trace


@dataclass
class Declared:
    """
    Optional context from the Advanced panel. Every field is optional and none
    of it is ever scored directly. The category is used only to compare against
    what the site's own content says.
    """
    legal_name: str = ""
    category: str = ""
    country: str = ""
    email: str = ""
    notes: str = ""

    def is_empty(self) -> bool:
        return not any([self.legal_name, self.category, self.country, self.email])


@dataclass
class Evidence:
    """Raw upstream responses, kept so a decision can be re-read months later."""
    rdap: dict = field(default_factory=dict)
    dns: dict = field(default_factory=dict)
    geoip: dict = field(default_factory=dict)
    tls: dict = field(default_factory=dict)
    reputation: dict = field(default_factory=dict)
    crawl: dict = field(default_factory=dict)


def _gather(domain: str, deadline: Deadline) -> Evidence:
    """
    Phase one: everything that can be fetched independently, in parallel.

    RDAP, DNS, the root page and the reputation lookup do not depend on each
    other, so they share one wall-clock window instead of four.
    """
    trace = deadline.trace
    evidence = Evidence()

    trace.event("enrich", "Enrichment fan-out", "info",
                "4 independent lookups on one wall-clock window — RDAP, DNS A, "
                f"storefront root, threat feed · {deadline.remaining():.1f}s of budget left")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "rdap": pool.submit(rdap_lookup, domain, deadline),
            "dns": pool.submit(resolve_a, domain, deadline),
            "root": pool.submit(fetch_root, domain, deadline),
            "reputation": pool.submit(sig_reputation.lookup, domain, deadline),
        }
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=max(0.1, deadline.remaining() + 1.0))
            except Exception as exc:
                results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                trace.event("enrich", f"{name} lookup", "error", f"{type(exc).__name__}: {exc}")

    evidence.rdap = results["rdap"]
    evidence.dns = results["dns"]
    evidence.reputation = results["reputation"]
    root = results["root"]

    ip = (evidence.dns or {}).get("ip")
    with trace.step("enrich", "GeoLite2 country lookup",
                    "local mmdb read — no outbound call, no rate limit") as step:
        evidence.geoip = geoip.country_for_ip(ip) if ip else {
            "ok": False, "error": (evidence.dns or {}).get("error", "no A record resolved")
        }
        if evidence.geoip.get("country"):
            step["detail"] = (f"{ip} → {evidence.geoip['country']} "
                              f"({evidence.geoip.get('country_name')})")
        else:
            step["status"] = "warn"
            step["detail"] = evidence.geoip.get("error") or f"{ip}: no country in the database"

    # TLS inspection and the depth-1 crawl both need the root result, and both
    # want whatever budget is left.
    trace.event("crawl", "Crawl phase", "info",
                "TLS inspection and the depth-1 crawl share the remaining "
                f"{deadline.remaining():.1f}s")
    with ThreadPoolExecutor(max_workers=2) as pool:
        tls_future = pool.submit(tls_info, domain, deadline)
        crawl_future = pool.submit(crawl_site, domain, root, deadline)
        try:
            evidence.tls = tls_future.result(timeout=max(0.1, deadline.remaining() + 1.0))
        except Exception as exc:
            evidence.tls = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            trace.event("enrich", "TLS inspection", "error", f"{type(exc).__name__}: {exc}")
        try:
            evidence.crawl = crawl_future.result(timeout=max(0.1, deadline.remaining() + 2.0))
        except Exception as exc:
            evidence.crawl = {"root_ok": False, "root_error": f"{type(exc).__name__}: {exc}"}
            trace.event("crawl", "Crawl", "error", f"{type(exc).__name__}: {exc}")

    return evidence


def _compute_signals(domain: str, declared: Declared, evidence: Evidence,
                     trace: Trace) -> list[Signal]:
    bundle = evidence.crawl or {}
    # Category is inferred from the pages that describe the offering, never from
    # the terms and privacy boilerplate. See crawl.BOILERPLATE_CLASSES.
    category_text = bundle.get("category_text") or bundle.get("combined_text", "")
    with trace.step("infer", "Acceptance-taxonomy inference",
                    f"scoring {len(category_text.split()):,} words of offering copy "
                    "against the category lexicon") as step:
        inference = infer_category(category_text)
        if inference.get("category"):
            top = (inference.get("ranked") or [{}])[0]
            runners = ", ".join(
                f"{r['category']} {r['hits']}" for r in (inference.get("ranked") or [])[1:4])
            step["status"] = "warn" if inference.get("tier") == "restricted" else "ok"
            step["detail"] = (
                f"{top.get('label') or inference['category']} · {inference.get('tier')} tier · "
                f"{inference.get('score')} lexicon hits"
                + (f" ({', '.join(inference.get('hits', [])[:3])})" if inference.get("hits") else "")
                + (f" · runners-up: {runners}" if runners else "")
                + ("" if inference.get("confident") else " · reading too thin to decline on"))
        else:
            step["status"] = "warn"
            step["detail"] = "no category matched the site's own copy"

    producers = [
        ("domain_age", lambda: sig_identity.domain_age(evidence.rdap)),
        ("registration_term", lambda: sig_identity.registration_term(evidence.rdap)),
        ("privacy_proxy", lambda: sig_identity.privacy_proxy(evidence.rdap)),
        ("tld_abuse", lambda: sig_identity.tld_abuse(domain)),
        ("http_root", lambda: sig_liveness.http_root(bundle)),
        ("tls", lambda: sig_liveness.tls(evidence.tls)),
        ("content_depth", lambda: sig_liveness.content_depth(bundle)),
        ("parked", lambda: sig_liveness.parked(bundle)),
        ("ttfb", lambda: sig_liveness.ttfb(bundle)),
        ("refund_policy", lambda: sig_commercial.refund_policy(bundle)),
        ("terms_page", lambda: sig_commercial.terms_page(bundle)),
        ("privacy_page", lambda: sig_commercial.privacy_page(bundle)),
        ("contact_page", lambda: sig_commercial.contact_page(bundle)),
        ("pricing_page", lambda: sig_commercial.pricing_page(bundle)),
        ("processor", lambda: sig_payment.processor(bundle)),
        ("price_point", lambda: sig_payment.price_point(bundle)),
        ("recurring_billing", lambda: sig_payment.recurring_billing(bundle)),
        ("category_tier", lambda: sig_category.category_tier(bundle, inference)),
        ("category_mismatch", lambda: sig_category.category_mismatch(
            declared.category or None, inference)),
        ("restricted_keywords", lambda: sig_category.restricted_keywords(bundle)),
        ("safe_browsing", lambda: sig_reputation.safe_browsing(evidence.reputation)),
        ("geo_consistency", lambda: sig_consistency.geo_consistency(
            declared.country or None, evidence.geoip, domain)),
        ("legal_name_match", lambda: sig_consistency.legal_name_match(
            declared.legal_name or None, bundle)),
        ("email_domain_match", lambda: sig_consistency.email_domain_match(
            declared.email or None, domain, bundle)),
    ]

    trace.event("signals", "Scoring the policy", "info",
                f"{len(producers)} signals across {len(CATEGORY_WEIGHTS)} categories, "
                f"{TOTAL_WEIGHT} weight points, no further network calls")

    signals = []
    for key, producer in producers:
        try:
            signal = producer()
        except Exception as exc:
            # A bug in one scorer must not take down the decision. It becomes an
            # unavailable signal like any other upstream failure.
            signal = unavailable(
                key, f"This signal raised an internal error and was excluded ({type(exc).__name__}: {exc}).")
            trace.event("signals", signal.label, "error",
                        f"{type(exc).__name__}: {exc} — dropped from the denominator")
        else:
            if signal.status == OK:
                trace.event("signals", signal.label, "ok",
                            f"{signal.raw} → {signal.normalized}/100 × {signal.weight} pts "
                            f"= {signal.contribution:.2f}"
                            + (f" · {', '.join(signal.codes)}" if signal.codes else ""),
                            signal=signal.key, normalized=signal.normalized,
                            weight=signal.weight, why=signal.reason)
            else:
                trace.event("signals", signal.label, "warn",
                            f"unavailable · −{signal.weight} pts off the denominator",
                            signal=signal.key, weight=signal.weight, why=signal.reason)
        signals.append(signal)
    return signals


def _score(signals: list[Signal]) -> dict:
    computed = [s for s in signals if s.status == OK and s.normalized is not None]
    denominator = sum(s.weight for s in computed)
    numerator = sum(s.contribution for s in computed)

    if denominator == 0:
        return {
            "score": None, "confidence": 0.0, "computed_weight": 0,
            "missing_weight": TOTAL_WEIGHT, "denominator": 0,
        }

    return {
        "score": round(numerator / denominator * 100, 1),
        "confidence": round(denominator / TOTAL_WEIGHT, 3),
        "computed_weight": denominator,
        "missing_weight": TOTAL_WEIGHT - denominator,
        "denominator": denominator,
        "raw_points": round(numerator, 2),
    }


def _by_category(signals: list[Signal]) -> list[dict]:
    out = []
    for cat, weight in CATEGORY_WEIGHTS.items():
        members = [s for s in signals if s.category == cat]
        computed = [s for s in members if s.status == OK and s.normalized is not None]
        available = sum(s.weight for s in computed)
        earned = sum(s.contribution for s in computed)
        out.append({
            "id": cat,
            "label": CATEGORY_LABELS[cat],
            "description": CATEGORY_DESCRIPTIONS[cat],
            "weight": weight,
            "available_weight": available,
            "earned": round(earned, 2),
            "normalized": round(earned / available * 100, 1) if available else None,
            "signals": [s.as_dict() for s in members],
        })
    return out


def evaluate(domain_input: str, declared: Declared | None = None,
             timeout: float = GLOBAL_TIMEOUT_S, trace: Trace | None = None) -> dict:
    """
    Run the full policy against one domain and return a complete decision.

    Never raises for network reasons. Raises InvalidDomain only when the input
    is not a domain at all, which is a form-validation error, not a verdict.

    Pass a `trace` to watch it happen; one is created either way and the events
    are returned under `trace`, so a decision read back out of the audit table
    still carries the calls that produced it.
    """
    from .decision import decide

    started = time.monotonic()
    declared = declared or Declared()
    trace = trace or Trace()
    domain = normalize_domain(domain_input)

    if domain != domain_input.strip():
        trace.event("input", "Normalised the input", "ok",
                    f"“{domain_input.strip()}” → {domain} (registrable domain, "
                    "public-suffix aware)")
    if not declared.is_empty():
        trace.event("input", "Declared context supplied", "info",
                    "compared against what the site says, never scored directly")

    deadline = Deadline(timeout, trace=trace)
    trace.event("input", "Evaluation started", "ok",
                f"{domain} · policy {POLICY_VERSION} · {timeout:.0f}s global budget")

    evidence = _gather(domain, deadline)
    signals = _compute_signals(domain, declared, evidence, trace)
    scoring = _score(signals)

    codes = []
    for signal in signals:
        for code in signal.codes:
            if code not in codes:
                codes.append(code)

    if scoring["confidence"] < CONFIDENCE_FLOOR and "LOW_CONFIDENCE" not in codes:
        codes.append("LOW_CONFIDENCE")

    trace.event(
        "score",
        "Weighted score" if scoring["score"] is not None else "Nothing could be scored",
        "ok" if scoring["score"] is not None else "error",
        (f"{scoring.get('raw_points', 0)} of {scoring['computed_weight']} available points "
         f"= {scoring['score']}/100 · confidence {round(scoring['confidence'] * 100)}% "
         f"({scoring['computed_weight']}/{TOTAL_WEIGHT} weight computed, "
         f"{scoring['missing_weight']} dropped)")
        if scoring["score"] is not None else
        "every signal in the policy failed to compute — held, not declined")

    if codes:
        trace.event("score", "Reason codes raised", "warn", " ".join(codes))

    decision = decide(scoring, codes, signals)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    signal_dicts = [s.as_dict() for s in signals]

    for override in decision.get("overrides_applied", []) or []:
        trace.event("decide", "Policy override", "warn",
                    f"{override.get('code')} caps the outcome at "
                    f"{override.get('capped_at') or decision['decision']} regardless of score")
    trace.event("decide", decision["decision"], "ok",
                f"band {decision['band']} · reserve "
                f"{decision['reserve_pct'] if decision['reserve_pct'] is not None else '—'}% · "
                f"payout {decision['payout'] or '—'} · decided in {elapsed_ms} ms"
                + (" · the global deadline expired mid-run" if deadline.expired() else ""))

    result = {
        "trace": trace.events,
        "domain": domain,
        "input": domain_input,
        "policy_version": POLICY_VERSION,
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_ms": elapsed_ms,
        "timed_out": deadline.expired(),
        "score": scoring["score"],
        "confidence": scoring["confidence"],
        "confidence_pct": round(scoring["confidence"] * 100),
        "computed_weight": scoring["computed_weight"],
        "missing_weight": scoring["missing_weight"],
        **decision,
        "reason_codes": [
            {"code": c, "description": REASON_CODES.get(c, "")} for c in codes
        ],
        "categories": _by_category(signals),
        "signals": signal_dicts,
        "unavailable": [
            {"key": s.key, "label": s.label, "weight": s.weight, "reason": s.reason}
            for s in signals if s.status != OK
        ],
        "declared": asdict(declared),
        "evidence": {
            "rdap": {k: v for k, v in (evidence.rdap or {}).items() if k != "body"},
            "dns": evidence.dns,
            "geoip": evidence.geoip,
            "tls": evidence.tls,
            "reputation": {k: v for k, v in (evidence.reputation or {}).items()
                           if k != "matches"},
            "geoip_database": geoip.database_info(),
            "crawl": {
                "root_url": (evidence.crawl or {}).get("root_url"),
                "root_status": (evidence.crawl or {}).get("root_status"),
                "title": (evidence.crawl or {}).get("title"),
                # The merchant's own one-line description of itself, kept
                # because it is the best sentence anyone will write about what
                # this business is.
                "description": (evidence.crawl or {}).get("description"),
                "site_name": (evidence.crawl or {}).get("site_name"),
                "headings": (evidence.crawl or {}).get("headings", [])[:6],
                "word_count": (evidence.crawl or {}).get("word_count"),
                "links_discovered": (evidence.crawl or {}).get("links_discovered"),
                "links_fetched": (evidence.crawl or {}).get("links_fetched"),
                "crawl_truncated": (evidence.crawl or {}).get("crawl_truncated"),
                "pages": (evidence.crawl or {}).get("pages", {}),
                "crawled": (evidence.crawl or {}).get("crawled", []),
            },
        },
        "weights_total": TOTAL_WEIGHT,
        "signal_count": len(SIGNAL_SPEC),
    }

    # Prose last, and strictly downstream of the verdict: it reads the finished
    # decision and cannot reach back into it.
    with trace.step("narrate", "Writing the plain-English summary",
                    "what this business appears to be, and why this decision — "
                    "no network call, no model") as step:
        result["summary"] = summarise(domain, result["evidence"], signal_dicts, result)
        step["detail"] = (f"{len(result['summary']['business']['paragraph'].split())} words on the "
                          f"business, {len(result['summary']['why']['paragraphs'])} paragraphs on "
                          f"the decision")

    # Re-read: the snapshot above was taken before the summary was written.
    result["trace"] = trace.events
    return result
