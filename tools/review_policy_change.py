#!/usr/bin/env python3
"""
Ask a model to review a policy change before it ships.

This is a *development* tool. It reviews the underwriting policy — the weights,
the thresholds, the override table, the code that implements them — and it is
the one place in this repository where a model is asked to form a view rather
than to reword one.

That distinction is the whole point, so it is worth stating plainly:

  mri/llm.py runs *inside* an evaluation. It never sees a merchant it can rule
  on: it is handed a decision that has already been made and asked to make the
  prose readable. A model never scores, overrides or reviews a merchant there.

  This tool runs *outside* any evaluation, on a developer's machine, against a
  git diff. Its subject is our own code. It cannot reach an evaluation, a score
  or a stored decision, and nothing it returns is written anywhere — it prints
  to a terminal for a human to read and argue with.

Keeping a model away from underwriting decisions does not mean keeping it away
from the engineering, and a second reader on a change to the scoring policy is
worth having.

Usage
-----
    export GEMINI_API_KEY=...            # or MRI_LLM_API_KEY + MRI_LLM_BASE_URL
    python tools/review_policy_change.py
    python tools/review_policy_change.py --base main --model gemini-2.5-pro
    python tools/review_policy_change.py --question "Is the new decline override too aggressive?"
    python tools/review_policy_change.py --dry-run          # print the prompt, call nothing

What it sends: the diff against a base ref, and a live outcome table produced by
running the current engine over every offline fixture in tests/fixtures.py. The
table is generated at call time rather than pasted in, so it cannot drift from
what the code actually does.

Requires network access to the model provider. In sandboxes that deny outbound
hosts this will fail at the request; --dry-run still works everywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MRI_DB_PATH", "/tmp/mri-review.db")
os.environ.setdefault("MRI_SKIP_WARM", "1")

SYSTEM = """You are a senior payments risk engineer reviewing a change to an automated merchant-underwriting policy before it ships. You have seen underwriting models fail in both directions and you respect both failure modes.

The two errors are not symmetric, and saying which one a change trades for the other is most of your job:

  A false approve boards a fraudulent merchant. The cost is chargebacks, scheme fines and, past a threshold, the acquirer's own processing rights.

  A false decline turns away a real business. The cost is revenue and reputation, it is usually silent, and nobody files a report about the merchant who was rejected and went to a competitor.

An automated *decline* deserves a higher bar than an automated *hold*, because a hold puts a human in the loop and a decline does not.

What is worth writing:
- Whether the change is sound, with the reason. If it is sound, say so plainly and briefly; do not manufacture objections to look thorough.
- Any concrete failure case: a class of legitimate merchant this newly harms, or a class of bad merchant it still lets through. Name the merchant shape and trace it through the actual numbers in the diff. A hypothetical you cannot ground in the diff is not worth raising.
- Whether a threshold or weight looks arbitrary, and what you would set it to instead.
- Whether the change is testing what it claims to test.

What is not worth writing: style notes, naming, praise, restating the diff back, or hedging every claim into uselessness. Assume the reader wrote this code and wants it broken, not admired.

Where you disagree with the change, say so directly and give the strongest version of the argument. Where you are uncertain, say which fact would settle it.

Structure your reply as:
  VERDICT — ship / ship with changes / do not ship, and one sentence saying why.
  FINDINGS — each one: what breaks, the merchant shape it breaks for, and what to do about it. Most serious first. If there are none, say so and stop.
  WHAT WOULD CHANGE MY MIND — the evidence you would want and do not have.

Prose and short lists. No preamble."""


# ── Evidence gathering ──────────────────────────────────────────────────────
def _fake_fetch_factory(site: dict):
    def _fetch(url, deadline, timeout=3.0):
        body = site.get(url) or site.get(url.rstrip("/")) or site.get(url + "/")
        if body is None:
            return {"ok": True, "status": 404, "url": url, "body": "<html>not found</html>",
                    "ttfb_ms": 40, "elapsed_ms": 45, "headers": {}}
        return {"ok": True, "status": 200, "url": url, "body": body,
                "ttfb_ms": 120, "elapsed_ms": 140, "headers": {}}
    return _fetch


def _run(domain: str, site: dict, rdap_data=None):
    """
    One evaluation with the four network entry points swapped for fixtures.

    Deliberately the same seams tests/test_engine.py mocks: the real parser,
    crawler, signal code, scorer and decision logic all run, so the table below
    reports what the engine does rather than what a stub says it does.
    """
    from mri.engine import Declared, evaluate
    from tests import fixtures

    fetch = _fake_fetch_factory(site)

    def fake_fetch_root(d, deadline):
        result = fetch(f"https://{d}/", deadline)
        result["attempted"] = f"https://{d}/"
        return result

    default_rdap = fixtures.rdap("2019-03-01T00:00:00Z", "2027-03-01T00:00:00Z")
    with mock.patch("mri.crawl.fetch", fetch), \
         mock.patch("mri.engine.fetch_root", fake_fetch_root), \
         mock.patch("mri.engine.rdap_lookup", lambda d, dl: rdap_data or default_rdap), \
         mock.patch("mri.engine.tls_info", lambda d, dl: fixtures.tls()), \
         mock.patch("mri.engine.resolve_a",
                    lambda d, dl: {"ok": True, "ip": "8.8.8.8", "all": ["8.8.8.8"]}), \
         mock.patch("mri.signals.reputation.lookup",
                    lambda d, dl: {"ok": True, "source": "Google Safe Browsing v4",
                                   "listed": False, "matches": []}):
        return evaluate(domain, Declared())


def outcome_table() -> list[dict]:
    """Run the current engine over every offline fixture and report what it did."""
    from tests import fixtures

    # A young, privacy-masked, one-year registration for the fixtures whose point
    # is a weak merchant; the default clean record would mask what they test.
    young = fixtures.rdap(fixtures.days_ago(250), fixtures.days_ahead(115), privacy=True)
    fresh = fixtures.rdap(fixtures.days_ago(10), fixtures.days_ahead(355), privacy=True)
    overrides = {"brochure.in": young, "shellco.top": fresh}

    rows = []
    for domain, site in sorted(fixtures.SITES.items()):
        result = _run(domain, site, rdap_data=overrides.get(domain))
        rows.append({
            "fixture": domain,
            "score": result["score"],
            "band": result["band"],
            "confidence": result["confidence"],
            "reason_codes": [c["code"] for c in result["reason_codes"]],
            "overrides_applied": [o["code"] for o in result.get("overrides_applied") or []],
            "homepage_word_count": result["evidence"]["crawl"].get("word_count"),
            "signals_that_abstained": [u["key"] for u in result["unavailable"]],
        })
    return rows


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=False).stdout


def gather(base: str, question: str | None) -> str:
    from mri import policy

    diff = git("diff", f"{base}...HEAD", "--", "mri/", "tests/")
    if not diff.strip():
        diff = git("diff", "--", "mri/", "tests/")
    log = git("log", "--format=%s%n%n%b", f"{base}..HEAD")

    weights = {k: {"category": c, "points": w}
               for k, (c, w, _) in policy.SIGNAL_SPEC.items()}

    parts = [
        f"POLICY VERSION UNDER REVIEW: {policy.POLICY_VERSION}",
        "",
        "SIGNAL WEIGHTS (100 points total, a signal that cannot be computed is "
        "removed from the weighted denominator rather than scored zero):",
        json.dumps(weights, indent=2),
        "",
        "DECISION BANDS: 80-100 auto approve · 60-79 approve with a 5% reserve · "
        "40-59 manual review by a person · 0-39 decline.",
        "",
        "BAND OVERRIDES (a code that caps the outcome regardless of score):",
        json.dumps(policy.BAND_OVERRIDES, indent=2),
        "",
        "COMMIT MESSAGES ON THIS BRANCH:",
        log.strip() or "(none)",
        "",
        "LIVE OUTCOME TABLE — the current code run over every offline fixture, "
        "generated just now rather than pasted in:",
        json.dumps(outcome_table(), indent=2, default=str),
        "",
        f"THE DIFF (against {base}):",
        "```diff",
        diff.rstrip(),
        "```",
    ]
    if question:
        parts += ["", "THE REVIEWER ESPECIALLY WANTS AN ANSWER ON THIS:", question]
    return "\n".join(parts)


# ── Providers ───────────────────────────────────────────────────────────────
def call_gemini(prompt: str, model: str, timeout: float) -> str:
    import httpx

    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"],
                 "content-type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            # Reasoning stays ON here, unlike the narration path in mri/llm.py.
            # That one rewrites finished sentences and has nothing to work out;
            # this one is being asked for a judgement and needs the room.
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8000},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        raise ValueError(f"no candidate returned (blockReason={blocked or 'none given'})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text.strip():
        raise ValueError(
            f"empty response (finishReason={candidates[0].get('finishReason') or 'none given'})")
    return text


def call_openai_compatible(prompt: str, model: str, timeout: float) -> str:
    import httpx

    response = httpx.post(
        os.environ["MRI_LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['MRI_LLM_API_KEY']}",
                 "content-type": "application/json"},
        json={"model": model, "temperature": 0.3, "max_tokens": 4000,
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip())
    parser.add_argument("--base", default="main", help="git ref to diff against (default: main)")
    parser.add_argument("--model", default=None,
                        help="override the model; gemini-2.5-pro reads a diff more carefully "
                             "than flash if your key has access to it")
    parser.add_argument("--question", default=None, help="a specific question to answer")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds (default: 180)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt and exit without calling anything")
    args = parser.parse_args()

    prompt = gather(args.base, args.question)

    if args.dry_run:
        print(f"--- SYSTEM ({len(SYSTEM):,} chars) ---\n{SYSTEM}\n")
        print(f"--- PROMPT ({len(prompt):,} chars) ---\n{prompt}")
        return 0

    if os.environ.get("GEMINI_API_KEY"):
        provider = "gemini"
        model = args.model or os.environ.get("MRI_GEMINI_MODEL", "gemini-2.5-flash")
        call = call_gemini
    elif os.environ.get("MRI_LLM_API_KEY") and os.environ.get("MRI_LLM_BASE_URL"):
        provider = "openai-compatible"
        model = args.model or os.environ.get("MRI_LLM_MODEL", "llama-3.3-70b-versatile")
        call = call_openai_compatible
    else:
        print(
            "No model configured.\n\n"
            "  export GEMINI_API_KEY=...        # Google AI Studio, free tier\n"
            "or\n"
            "  export MRI_LLM_API_KEY=... MRI_LLM_BASE_URL=...   # any OpenAI-compatible endpoint\n\n"
            "Note that this reads the environment of the shell it runs in. A key set in a\n"
            "Render dashboard or a deployment secret is not visible here — run it where the\n"
            "key actually lives, or export it into this shell first.\n\n"
            "Use --dry-run to see exactly what would be sent.",
            file=sys.stderr)
        return 2

    print(f"Reviewing {git('rev-parse', '--abbrev-ref', 'HEAD').strip()} against {args.base} "
          f"· {provider}:{model} · {len(prompt):,} chars of context\n", file=sys.stderr)
    try:
        print(call(prompt, model, args.timeout))
    except Exception as exc:
        print(f"\nThe review call failed: {type(exc).__name__}: {exc}\n\n"
              "If this is a 403 or a connection error, the network is blocking the provider\n"
              "rather than the key being wrong. --dry-run prints the prompt so you can paste\n"
              "it into a chat window instead.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
