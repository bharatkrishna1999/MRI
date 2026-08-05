"""HTTP surface: the single-field UI, the benchmark page, and the JSON API."""
from __future__ import annotations

import json
import os
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import benchmark as bench
from . import geo, store
from .domains import InvalidDomain
from .engine import Declared
from .policy import (
    CATEGORY_DESCRIPTIONS,
    CATEGORY_LABELS,
    CATEGORY_WEIGHTS,
    DECISION_BANDS,
    GLOBAL_TIMEOUT_S,
    PER_CALL_TIMEOUT_S,
    POLICY_EFFECTIVE,
    POLICY_VERSION,
    REASON_CODES,
    SIGNAL_SPEC,
)
from .service import DEMO_DOMAINS, run, warm_demos_async, warm_status
from .taxonomy import CATEGORY_CHOICES

UI_DIR = Path(__file__).resolve().parent / "ui"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-warm the demo domains so the three buttons above the input are instant.
    # Nobody should watch a live crawl in the first minute of a call.
    if os.environ.get("MRI_SKIP_WARM", "").lower() not in ("1", "true", "yes"):
        warm_demos_async()
    yield


app = FastAPI(
    title="Merchant Risk Intelligence",
    version=POLICY_VERSION,
    description="Domain-first underwriting for a merchant of record.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class AdvancedInput(BaseModel):
    """Everything in the collapsed Advanced panel. All of it optional."""
    legal_name: str = ""
    category: str = ""
    country: str = ""
    email: str = ""
    notes: str = ""


class EvaluateRequest(BaseModel):
    domain: str = Field(..., description="example.com or https://www.example.com/pricing")
    advanced: AdvancedInput = AdvancedInput()
    refresh: bool = False


def _declared(advanced: AdvancedInput) -> Declared:
    return Declared(
        legal_name=advanced.legal_name.strip(),
        category=advanced.category.strip(),
        country=advanced.country.strip().upper(),
        email=advanced.email.strip(),
        notes=advanced.notes.strip(),
    )


def _base_url(request: Request | None) -> str:
    """
    The curl printed on the result page has to be the one that actually works,
    so it is built from the host the caller reached us on unless an explicit
    public URL is configured. Resolved eagerly, because the streaming endpoint
    finishes its work on a worker thread after the request scope is gone.
    """
    base = os.environ.get("MRI_PUBLIC_URL", "").rstrip("/")
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    return base or "http://localhost:8000"


def _with_curl(result: dict, base: str) -> dict:
    result["api"] = {
        "url": f"{base}/api/v1/evaluate?domain={result['domain']}",
        "curl": f"curl -s '{base}/api/v1/evaluate?domain={result['domain']}' | jq",
    }
    return result


# ── Evaluation ──────────────────────────────────────────────────────────────
@app.post("/api/v1/evaluate")
def evaluate_post(payload: EvaluateRequest, request: Request):
    try:
        result = run(payload.domain, _declared(payload.advanced),
                     use_cache=not payload.refresh)
    except InvalidDomain as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _with_curl(result, _base_url(request))


@app.get("/api/v1/evaluate")
def evaluate_get(
    request: Request,
    domain: str = Query(..., description="Domain or URL to underwrite"),
    legal_name: str = "",
    category: str = "",
    country: str = "",
    email: str = "",
    refresh: bool = False,
):
    """The full structured decision as JSON. This is the integration surface."""
    try:
        result = run(
            domain,
            Declared(legal_name=legal_name, category=category,
                     country=country.upper(), email=email),
            use_cache=not refresh,
        )
    except InvalidDomain as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _with_curl(result, _base_url(request))


# ── Live evaluation stream ──────────────────────────────────────────────────
def _sse(event: str, payload) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@app.get("/api/v1/evaluate/stream")
def evaluate_stream(
    request: Request,
    domain: str = Query(..., description="Domain or URL to underwrite"),
    legal_name: str = "",
    category: str = "",
    country: str = "",
    email: str = "",
    refresh: bool = False,
):
    """
    The same evaluation as GET /api/v1/evaluate, narrated as it happens.

    Server-sent events. Every outbound call, every page fetched and every signal
    scored arrives as a `trace` event the moment it is recorded; the finished
    decision arrives once as `result`, or a `failed` event if the input was not a
    domain. The event names avoid `open` and `error`, which EventSource already
    dispatches for transport state. Work that has not finished emits a
    `running` event first and a terminal event carrying the same `id` after, so
    a consumer can show a line, then complete it in place.

        curl -N 'http://localhost:8000/api/v1/evaluate/stream?domain=stripe.com'
    """
    declared = Declared(legal_name=legal_name, category=category,
                        country=country.upper(), email=email)
    # Read off the request before the generator starts: by the time the worker
    # thread runs, the request scope may be gone.
    base = _base_url(request)

    def stream():
        events: queue.Queue = queue.Queue()
        SENTINEL = object()

        def worker():
            try:
                result = run(domain, declared, use_cache=not refresh,
                             sink=events.put)
                events.put(("result", _with_curl(result, base)))
            except InvalidDomain as exc:
                events.put(("failed", {"detail": str(exc), "status": 400}))
            except Exception as exc:
                events.put(("failed", {"detail": f"{type(exc).__name__}: {exc}",
                                       "status": 500}))
            finally:
                events.put(SENTINEL)

        threading.Thread(target=worker, name=f"stream-{domain}", daemon=True).start()

        yield _sse("start", {"domain": domain, "policy_version": POLICY_VERSION})
        while True:
            item = events.get()
            if item is SENTINEL:
                break
            if isinstance(item, tuple):
                yield _sse(item[0], item[1])
            else:
                yield _sse("trace", item)
        yield _sse("done", {"ok": True})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx and most PaaS proxies buffer responses by default, which
            # would hold every event until the run finished — exactly the thing
            # this endpoint exists to avoid.
            "X-Accel-Buffering": "no",
        },
    )


# ── Demos, policy, audit ────────────────────────────────────────────────────
@app.get("/api/v1/demos")
def demos():
    return warm_status()


@app.get("/api/v1/policy")
def policy():
    return {
        "policy_version": POLICY_VERSION,
        "effective": POLICY_EFFECTIVE,
        "global_timeout_s": GLOBAL_TIMEOUT_S,
        "per_call_timeout_s": PER_CALL_TIMEOUT_S,
        "category_weights": CATEGORY_WEIGHTS,
        "category_labels": CATEGORY_LABELS,
        "category_descriptions": CATEGORY_DESCRIPTIONS,
        "signals": [
            {"key": key, "category": cat, "weight": weight, "label": label}
            for key, (cat, weight, label) in SIGNAL_SPEC.items()
        ],
        "bands": DECISION_BANDS,
        "reason_codes": REASON_CODES,
        "taxonomy": CATEGORY_CHOICES,
        "geoip": geo.database_info(),
        "safe_browsing_key_configured": bool(os.environ.get("SAFE_BROWSING_API_KEY")),
    }


@app.get("/api/v1/taxonomy")
def taxonomy():
    return {"categories": CATEGORY_CHOICES}


@app.get("/api/v1/audit/recent")
def audit_recent(limit: int = 25):
    return {"policy_version": POLICY_VERSION, "runs": store.recent(limit),
            "stats": store.stats()}


@app.get("/api/v1/audit/{audit_id}")
def audit_get(audit_id: int):
    result = store.get(audit_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No evaluation with id {audit_id}")
    return result


@app.get("/api/v1/health")
def health():
    return {
        "ok": True,
        "policy_version": POLICY_VERSION,
        "geoip": geo.database_info(),
        "store": store.stats(),
        "demo_warm": warm_status()["status"],
    }


# ── Benchmark ───────────────────────────────────────────────────────────────
@app.get("/api/v1/benchmark/results")
def benchmark_results():
    payload = bench.load_results()
    if payload is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "No benchmark run stored yet.",
                "labels": len(bench.load_labels()),
                "hint": "POST /api/v1/benchmark/run, or run python -m mri.benchmark",
            },
        )
    return payload


@app.get("/api/v1/benchmark/labels")
def benchmark_labels():
    rows = bench.load_labels()
    return {
        "total": len(rows),
        "good": sum(1 for r in rows if r["true_label"] == "good"),
        "bad": sum(1 for r in rows if r["true_label"] == "bad"),
        "rows": rows,
    }


@app.post("/api/v1/benchmark/run")
def benchmark_run():
    return bench.start_background_run()


@app.get("/api/v1/benchmark/status")
def benchmark_status():
    return bench.run_state()


# ── Pages ───────────────────────────────────────────────────────────────────
def _page(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index():
    return _page("index.html")


@app.get("/benchmark", response_class=HTMLResponse)
def benchmark_page():
    return _page("benchmark.html")
