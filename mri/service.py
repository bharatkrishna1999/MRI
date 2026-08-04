"""
The layer the API talks to: cache, run, persist, warm.

Demo domains are pre-warmed on startup so the three buttons above the input are
instant. Nobody should watch a live crawl during the first minute of a call.
"""
from __future__ import annotations

import threading
import time

from .engine import Declared, evaluate
from .policy import CACHE_TTL_S, GLOBAL_TIMEOUT_S
from .store import cached, record

# T8 — three prefilled demos, one per outcome we want to be able to show.
DEMO_DOMAINS = [
    {
        "id": "clean",
        "domain": "stripe.com",
        "label": "Clean approve",
        "hint": "Long-lived domain, full policy pages, incumbent processor, standard category.",
        "expected": "auto_approve",
    },
    {
        "id": "borderline",
        "domain": "gumroad.com",
        "label": "Borderline review",
        "hint": "Real marketplace holding third-party funds — elevated tier, reserve territory.",
        "expected": "approve_with_reserve",
    },
    {
        # A real, large, entirely legal company that a merchant of record still
        # cannot board. Chosen over a fraud site on purpose: the decline is
        # driven by the acceptance policy reading the site's own content, which
        # needs no API key and cannot go stale between now and the demo.
        "id": "decline",
        "domain": "coinbase.com",
        "label": "Clear decline",
        "hint": "Reads as crypto from its own copy — restricted tier, not boardable at any score.",
        "expected": "decline",
    },
]

_warm_state = {"status": "cold", "started": None, "finished": None, "results": {}}
_warm_lock = threading.Lock()


def run(domain_input: str, declared: Declared | None = None,
        use_cache: bool = True, timeout: float = GLOBAL_TIMEOUT_S) -> dict:
    """
    Evaluate a domain, preferring a cached decision under 24 hours old.

    The cache key is the normalised domain plus the policy version. Declared
    context does not participate in the key when it is empty, which is the
    normal case: the single-field form sends nothing but a domain.
    """
    from .domains import normalize_domain

    domain = normalize_domain(domain_input)
    declared = declared or Declared()

    if use_cache and declared.is_empty():
        hit = cached(domain)
        if hit:
            return hit

    result = evaluate(domain_input, declared, timeout=timeout)
    result["cached"] = False
    try:
        result["audit_id"] = record(result)
    except Exception as exc:
        result["audit_id"] = None
        result["persistence_error"] = f"{type(exc).__name__}: {exc}"
    return result


def warm_demos_async() -> None:
    """Fire the three demo evaluations in the background at startup."""
    with _warm_lock:
        if _warm_state["status"] in ("warming", "ready"):
            return
        _warm_state["status"] = "warming"
        _warm_state["started"] = time.time()

    def _worker():
        for demo in DEMO_DOMAINS:
            try:
                result = run(demo["domain"])
                _warm_state["results"][demo["id"]] = {
                    "domain": demo["domain"],
                    "decision": result.get("decision"),
                    "score": result.get("score"),
                    "ok": True,
                }
            except Exception as exc:
                _warm_state["results"][demo["id"]] = {
                    "domain": demo["domain"], "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        _warm_state["status"] = "ready"
        _warm_state["finished"] = time.time()

    threading.Thread(target=_worker, name="demo-warm", daemon=True).start()


def warm_status() -> dict:
    ready = []
    for demo in DEMO_DOMAINS:
        hit = None
        try:
            hit = cached(demo["domain"])
        except Exception:
            pass
        ready.append({
            "id": demo["id"],
            "domain": demo["domain"],
            "label": demo["label"],
            "hint": demo["hint"],
            "cached": bool(hit),
            "decision": hit.get("decision") if hit else None,
            "score": hit.get("score") if hit else None,
        })
    return {
        "status": _warm_state["status"],
        "ttl_seconds": CACHE_TTL_S,
        "demos": ready,
    }
