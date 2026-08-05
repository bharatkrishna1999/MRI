"""
Run trace.

An underwriting decision that arrives as a number with a spinner in front of it
is indistinguishable from a guess. This module records what the engine actually
did — every outbound call, every page fetched, every signal scored — as an
ordered stream of events with wall-clock offsets and durations.

Two consumers, one recorder:

  * the JSON API embeds `trace` in every result, so a decision replayed from the
    audit table six months later still shows the calls that produced it;
  * the SSE endpoint attaches a sink and forwards each event the moment it is
    recorded, so the UI shows the run happening instead of a progress bar.

Long-running work is recorded as a pair: a `running` event when it starts and a
terminal event carrying the same `id` when it finishes. A consumer keys lines by
`id` and replaces the pending line in place, which is what makes the console
read like a live log rather than a wall of text.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

# Phases, in the order they occur. The UI groups the console by these.
PHASES = {
    "input": "Input",
    "cache": "Cache",
    "enrich": "Enrichment",
    "crawl": "Crawl",
    "infer": "Classification",
    "signals": "Signals",
    "score": "Scoring",
    "decide": "Decision",
    "persist": "Audit",
}

RUNNING = "running"
OK = "ok"
WARN = "warn"
ERROR = "error"
INFO = "info"


class Trace:
    """
    Thread-safe event recorder for one evaluation.

    The engine fans out across two thread pools, so events arrive out of order
    with respect to the code that emits them. `seq` is allocated under the lock
    and is the only ordering anyone should trust; `t_ms` is an offset from the
    start of the run and is what the console prints.
    """

    def __init__(self, sink=None):
        self._sink = sink
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._seq = 0
        self.started = time.monotonic()

    # ── recording ───────────────────────────────────────────────────────────
    def event(self, phase: str, label: str, status: str = INFO,
              detail: str = "", **extra) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "t_ms": int((time.monotonic() - self.started) * 1000),
                "phase": phase,
                "label": label,
                "status": status,
                "detail": detail,
                **extra,
            }
            self._events.append(event)
        self._emit(event)
        return event

    def _emit(self, event: dict) -> None:
        if self._sink is None:
            return
        try:
            self._sink(event)
        except Exception:
            # A broken consumer — a disconnected browser, usually — must never
            # take down the evaluation it is watching.
            pass

    @contextmanager
    def step(self, phase: str, label: str, detail: str = ""):
        """
        Record a unit of work that takes measurable time.

        Yields a mutable dict; set `detail` and optionally `status` on it inside
        the block and the terminal event carries them:

            with trace.step("enrich", "RDAP lookup") as step:
                result = rdap_lookup(...)
                step["detail"] = "registered 2011-03-01"
        """
        start = self.event(phase, label, RUNNING, detail)
        started = time.monotonic()
        outcome = {"detail": "", "status": OK}
        try:
            yield outcome
        except Exception as exc:
            self.event(phase, label, ERROR, f"{type(exc).__name__}: {exc}",
                       ref=start["id"], ms=int((time.monotonic() - started) * 1000))
            raise
        self.event(phase, label, outcome.get("status") or OK,
                   outcome.get("detail") or "", ref=start["id"],
                   ms=int((time.monotonic() - started) * 1000))

    # ── reading ─────────────────────────────────────────────────────────────
    @property
    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def as_list(self) -> list[dict]:
        return self.events


def fmt_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
