"""HTTP surface: the single-field UI, the benchmark page, and the JSON API."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
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


def _with_curl(result: dict, request: Request | None = None) -> dict:
    """
    The curl printed on the result page has to be the one that actually works,
    so it is built from the host the caller reached us on unless an explicit
    public URL is configured.
    """
    base = os.environ.get("MRI_PUBLIC_URL", "").rstrip("/")
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    base = base or "http://localhost:8000"
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
    return _with_curl(result, request)


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
    return _with_curl(result, request)


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
