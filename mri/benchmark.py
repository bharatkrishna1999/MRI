"""
Benchmark harness.

Runs the live engine over every domain in the labelled CSV and reports the
numbers an underwriting model is actually judged on: precision, recall, F1 and
false positive rate at a chosen approval threshold, plus the raw per-domain
scores so the page can recompute all of it as the threshold moves.

Convention throughout: the *positive class is bad*. Precision answers "when we
declined, how often were we right", recall answers "of everything we should
have declined, how much did we catch", and false positive rate answers "how
many good merchants did we turn away" — the number that costs revenue.
"""
from __future__ import annotations

import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .policy import POLICY_VERSION
from .store import latest_benchmark, save_benchmark

LABELS_PATH = Path(__file__).resolve().parent.parent / "data" / "benchmark_labels.csv"
RESULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "benchmark_results.json"

DEFAULT_THRESHOLD = 60  # scores at or above this are treated as approvable

_run_state = {"status": "idle", "done": 0, "total": 0, "started": None, "error": None}
_run_lock = threading.Lock()


def load_labels(path: Path = LABELS_PATH) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["true_label"] = row["true_label"].strip().lower()
        row["domain"] = row["domain"].strip().lower()
    return rows


def confusion(rows: list[dict], threshold: float) -> dict:
    """
    Positive class = bad. A domain scoring below the threshold is a predicted bad.
    Rows the engine could not score at all are reported separately rather than
    silently counted as either class.
    """
    tp = fp = tn = fn = 0
    unscored = 0
    for row in rows:
        score = row.get("score")
        if score is None:
            unscored += 1
            continue
        predicted_bad = score < threshold
        actually_bad = row["true_label"] == "bad"
        if predicted_bad and actually_bad:
            tp += 1
        elif predicted_bad and not actually_bad:
            fp += 1
        elif not predicted_bad and not actually_bad:
            tn += 1
        else:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    accuracy = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) else 0.0

    return {
        "threshold": threshold,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "unscored": unscored,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_positive_rate": round(fpr, 4),
        "accuracy": round(accuracy, 4),
    }


def sweep(rows: list[dict], step: int = 1) -> list[dict]:
    """Metrics at every threshold, so the slider has nothing to compute live."""
    return [confusion(rows, t) for t in range(0, 101, step)]


def evaluate_all(rows: list[dict] | None = None, workers: int = 6,
                 progress=None) -> dict:
    """
    Run the engine over the whole labelled set. Makes live network calls; one
    pass over 60 domains takes a few minutes depending on how many sites stall.
    """
    from .service import run

    rows = rows or load_labels()
    results = []
    started = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, row["domain"]): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            record = {
                "domain": row["domain"],
                "true_label": row["true_label"],
                "source": row.get("source", ""),
                "date_labelled": row.get("date_labelled", ""),
            }
            try:
                result = future.result()
                record.update({
                    "score": result.get("score"),
                    "confidence": result.get("confidence"),
                    "decision": result.get("decision"),
                    "band": result.get("band"),
                    "reason_codes": [c["code"] for c in result.get("reason_codes", [])],
                    "elapsed_ms": result.get("elapsed_ms"),
                    "error": None,
                })
            except Exception as exc:
                record.update({
                    "score": None, "confidence": 0, "decision": None, "band": None,
                    "reason_codes": [], "elapsed_ms": None,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            results.append(record)
            done += 1
            if progress:
                progress(done, len(rows))

    results.sort(key=lambda r: (r["true_label"], r["domain"]))
    payload = {
        "policy_version": POLICY_VERSION,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": round(time.time() - started, 1),
        "labels_file": LABELS_PATH.name,
        "total": len(results),
        "good": sum(1 for r in results if r["true_label"] == "good"),
        "bad": sum(1 for r in results if r["true_label"] == "bad"),
        "scored": sum(1 for r in results if r["score"] is not None),
        "results": results,
        "default_threshold": DEFAULT_THRESHOLD,
        "metrics": confusion(results, DEFAULT_THRESHOLD),
        "sweep": sweep(results),
        "histogram": histogram(results),
    }
    return payload


def histogram(rows: list[dict], bins: int = 10) -> dict:
    """Overlapping score distributions for the good and bad classes."""
    width = 100 / bins
    good = [0] * bins
    bad = [0] * bins
    for row in rows:
        score = row.get("score")
        if score is None:
            continue
        index = min(bins - 1, int(score // width))
        if row["true_label"] == "bad":
            bad[index] += 1
        else:
            good[index] += 1
    return {
        "bins": bins,
        "width": width,
        "edges": [round(i * width) for i in range(bins + 1)],
        "good": good,
        "bad": bad,
    }


# ── Persistence and background execution ────────────────────────────────────
def load_results() -> dict | None:
    """Committed results file first, then the newest run stored in SQLite."""
    import json

    stored = latest_benchmark()
    if stored:
        return stored
    if RESULTS_PATH.exists():
        try:
            with open(RESULTS_PATH, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return None
    return None


def save_results(payload: dict) -> None:
    import json

    save_benchmark(payload)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)


def run_state() -> dict:
    return dict(_run_state)


def start_background_run() -> dict:
    with _run_lock:
        if _run_state["status"] == "running":
            return dict(_run_state)
        rows = load_labels()
        _run_state.update({"status": "running", "done": 0, "total": len(rows),
                           "started": time.time(), "error": None})

    def _worker():
        try:
            def progress(done, total):
                _run_state["done"] = done
                _run_state["total"] = total

            payload = evaluate_all(progress=progress)
            save_results(payload)
            _run_state.update({"status": "complete", "finished": time.time()})
        except Exception as exc:
            _run_state.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=_worker, name="benchmark", daemon=True).start()
    return dict(_run_state)


if __name__ == "__main__":
    import json
    import sys

    print(f"Running {POLICY_VERSION} over {LABELS_PATH}...", file=sys.stderr)

    def _progress(done, total):
        print(f"  {done}/{total}", end="\r", file=sys.stderr)

    payload = evaluate_all(progress=_progress)
    save_results(payload)
    metrics = payload["metrics"]
    print(f"\n\nthreshold {metrics['threshold']}  (positive class = bad)")
    print(f"  precision            {metrics['precision']:.3f}")
    print(f"  recall               {metrics['recall']:.3f}")
    print(f"  f1                   {metrics['f1']:.3f}")
    print(f"  false positive rate  {metrics['false_positive_rate']:.3f}")
    print(f"  tp {metrics['tp']}  fp {metrics['fp']}  tn {metrics['tn']}  fn {metrics['fn']}")
    print(f"\nwritten to {RESULTS_PATH}")
