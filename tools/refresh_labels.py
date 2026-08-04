#!/usr/bin/env python3
"""
Maintain data/benchmark_labels.csv.

Two jobs:

  verify   Re-check every labelled domain and report the ones whose label no
           longer looks right — a "parked" row that now serves a real store, an
           "expired shell" that got bought and revived. Labels rot; a benchmark
           nobody re-verifies stops meaning anything.

  urlhaus  Pull the live URLhaus feed from abuse.ch and append fresh
           phishing/malware hosts to the bad set. Free, no key.

Usage:
    python tools/refresh_labels.py verify
    python tools/refresh_labels.py urlhaus --count 10
    python tools/refresh_labels.py urlhaus --count 10 --write
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mri.benchmark import LABELS_PATH, load_labels  # noqa: E402
from mri.domains import InvalidDomain, normalize_domain  # noqa: E402

URLHAUS_CSV = "https://urlhaus.abuse.ch/downloads/csv/"

# A row's label is questionable when the engine's own findings contradict it.
BAD_EVIDENCE = {"PARKED_DOMAIN", "SITE_UNREACHABLE", "CATEGORY_RESTRICTED",
                "SAFEBROWSING_HIT", "THIN_CONTENT"}


def verify() -> int:
    from mri.service import run

    rows = load_labels()
    suspect = []
    print(f"Verifying {len(rows)} labels against {LABELS_PATH.name}\n")
    for row in rows:
        try:
            result = run(row["domain"], use_cache=False)
        except Exception as exc:
            print(f"  ?? {row['domain']:32} evaluation failed: {exc}")
            continue

        codes = {c["code"] for c in result.get("reason_codes", [])}
        score = result.get("score")
        evidence = codes & BAD_EVIDENCE
        note = ""

        if row["true_label"] == "bad" and not evidence and (score or 0) >= 70:
            note = f"labelled bad but scores {score} with no adverse evidence"
        if row["true_label"] == "good" and (score or 100) < 40:
            note = f"labelled good but scores {score} ({', '.join(sorted(evidence)) or 'no codes'})"

        flag = "!!" if note else "ok"
        print(f"  {flag} {row['domain']:32} {row['true_label']:5} {str(score):>5}  {note}")
        if note:
            suspect.append((row["domain"], note))

    print(f"\n{len(suspect)} row(s) need a human to re-read the label.")
    for domain, note in suspect:
        print(f"  {domain}: {note}")
    return 1 if suspect else 0


def urlhaus(count: int, write: bool) -> int:
    import httpx

    from mri.netcalls import HEADERS

    print(f"Fetching {URLHAUS_CSV} ...")
    with httpx.Client(timeout=60, headers=HEADERS, follow_redirects=True) as client:
        response = client.get(URLHAUS_CSV)
    response.raise_for_status()

    body = response.content
    if body[:2] == b"PK":  # the feed is served zipped
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            body = archive.read(archive.namelist()[0])
    text = body.decode("utf-8", errors="replace")

    existing = {r["domain"] for r in load_labels()}
    fresh: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = next(csv.reader([line]), [])
        if len(parts) < 3:
            continue
        try:
            domain = normalize_domain(parts[2])
        except (InvalidDomain, IndexError):
            continue
        if domain in existing or domain in fresh:
            continue
        fresh.append(domain)
        if len(fresh) >= count:
            break

    today = time.strftime("%Y-%m-%d")
    print(f"\n{len(fresh)} new host(s) not already labelled:\n")
    new_rows = [[d, "bad", "urlhaus", today] for d in fresh]
    for row in new_rows:
        print("  " + ",".join(row))

    if not write:
        print("\nDry run. Re-run with --write to append these to the CSV.")
        return 0

    with open(LABELS_PATH, "a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(new_rows)
    print(f"\nAppended {len(new_rows)} row(s) to {LABELS_PATH}.")
    print("Re-run the benchmark so the metrics reflect the new labels:")
    print("    python -m mri.benchmark")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="re-check every committed label")
    feed = sub.add_parser("urlhaus", help="append fresh URLhaus hosts to the bad set")
    feed.add_argument("--count", type=int, default=10)
    feed.add_argument("--write", action="store_true")

    args = parser.parse_args()
    if args.command == "verify":
        return verify()
    return urlhaus(args.count, args.write)


if __name__ == "__main__":
    raise SystemExit(main())
