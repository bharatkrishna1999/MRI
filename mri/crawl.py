"""
Link discovery.

Fetch the root, pull every anchor, classify each one by both its href and its
anchor text, then fetch up to 15 internal links at depth 1 in a thread pool with
a 3 second timeout each. Policy pages are fetched before filler pages, because
the refund policy is worth more than another marketing page.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .netcalls import Deadline, fetch, internal_links, parse_html
from .policy import CRAWL_WORKERS, MAX_INTERNAL_LINKS, PER_CALL_TIMEOUT_S

# Page classes we care about, in priority order. Each pattern is applied to the
# anchor text and to the href path; either can match.
PAGE_PATTERNS = [
    ("refund", re.compile(
        r"refund|returns?\b|cancellation|cancel[\s-]?policy|money[\s-]?back|"
        r"\bexchanges?\b|shipping[\s-]and[\s-]returns", re.I)),
    ("terms", re.compile(
        r"terms|\btos\b|terms[\s-]of[\s-](service|use)|conditions|"
        r"\beula\b|legal\b|user[\s-]agreement", re.I)),
    ("privacy", re.compile(r"privacy|data[\s-]protection|\bgdpr\b|cookie[\s-]policy", re.I)),
    ("contact", re.compile(
        r"contact|support|help[\s-]?(center|centre|desk)?\b|get[\s-]in[\s-]touch|"
        r"reach[\s-]us|customer[\s-]service", re.I)),
    ("pricing", re.compile(r"pricing|\bplans?\b|\bprice|subscribe|checkout|\bbuy\b|upgrade", re.I)),
    ("about", re.compile(r"\babout\b|our[\s-]story|who[\s-]we[\s-]are|company|imprint|impressum", re.I)),
]

PAGE_CLASSES = [name for name, _ in PAGE_PATTERNS]

# Pages that state the contract rather than the offering. They are worth
# fetching — commercial legitimacy is scored on whether they exist — but they
# are not evidence of what the merchant sells, and on a payments or marketplace
# merchant the acceptable-use page is an inventory of the verticals it refuses.
# Feeding that to category inference classifies a merchant as its own blocklist.
BOILERPLATE_CLASSES = {"terms", "privacy", "refund"}


def classify_link(link: dict) -> list[str]:
    """A link can serve two purposes; 'Terms & Refunds' is one page, two classes."""
    from urllib.parse import urlparse

    path = urlparse(link["url"]).path or "/"
    haystack_text = link.get("text", "")
    haystack_href = f"{path} {link.get('href', '')}"
    classes = []
    for name, pattern in PAGE_PATTERNS:
        if pattern.search(haystack_text) or pattern.search(haystack_href):
            classes.append(name)
    return classes


def _select_targets(links: list[dict]) -> list[dict]:
    """
    Rank the crawl queue: pages that answer a policy question first, then a few
    ordinary pages so category inference has more than the homepage to read.
    """
    scored = []
    for link in links:
        classes = classify_link(link)
        if classes:
            priority = min(PAGE_CLASSES.index(c) for c in classes)
        else:
            priority = len(PAGE_CLASSES) + 1
        link = {**link, "classes": classes}
        scored.append((priority, link))

    scored.sort(key=lambda item: item[0])

    chosen, seen_classes = [], set()
    # First pass: one page per class, so we never spend all 15 slots on pricing.
    for priority, link in scored:
        if not link["classes"]:
            continue
        if all(c in seen_classes for c in link["classes"]):
            continue
        seen_classes.update(link["classes"])
        chosen.append(link)
        if len(chosen) >= MAX_INTERNAL_LINKS:
            return chosen
    # Second pass: fill the remaining budget with anything else on the domain.
    for _, link in scored:
        if link in chosen:
            continue
        chosen.append(link)
        if len(chosen) >= MAX_INTERNAL_LINKS:
            break
    return chosen


def crawl_site(domain: str, root: dict, deadline: Deadline) -> dict:
    """
    Build the evidence bundle the signal functions read from.

    `root` is the already-fetched root response so the caller can run the root
    fetch in parallel with RDAP and DNS.
    """
    bundle = {
        "root_ok": bool(root.get("ok")) and root.get("status", 0) < 400,
        "root_status": root.get("status"),
        "root_url": root.get("url") or root.get("attempted"),
        "root_error": root.get("error"),
        "root_error_kind": root.get("error_kind"),
        "ttfb_ms": root.get("ttfb_ms"),
        "root_html": "",
        "root_text": "",
        "title": "",
        "word_count": 0,
        "pages": {},
        "pages_found": {},
        "crawled": [],
        "links_discovered": 0,
        "links_fetched": 0,
        "combined_text": "",
        "combined_html": "",
        "category_text": "",
        "emails": [],
        "crawl_truncated": False,
    }

    trace = deadline.trace

    if not bundle["root_ok"]:
        trace.event("crawl", "Crawl skipped", "error",
                    f"the root did not serve a page ({bundle['root_error'] or bundle['root_status']}), "
                    "so there is nothing to walk")
        return bundle

    html = root.get("body", "")
    parsed = parse_html(html)
    bundle["root_html"] = html
    bundle["root_text"] = parsed["text"]
    bundle["title"] = parsed["title"]
    bundle["word_count"] = parsed["word_count"]
    trace.event("crawl", "Parsed the root page", "ok",
                f"“{parsed['title'] or 'untitled'}” · {parsed['word_count']:,} words · "
                f"{len(parsed['anchors'])} anchors")

    links = internal_links(bundle["root_url"], domain, parsed["anchors"])
    bundle["links_discovered"] = len(links)
    trace.event("crawl", "Link discovery", "ok",
                f"{len(links)} unique internal links on {domain} after dropping assets, "
                "mailto/tel and off-domain anchors")

    # A link found on the homepage is evidence the page exists even if we run
    # out of budget before fetching it. Record that separately from a fetch.
    for link in links:
        for cls in classify_link(link):
            bundle["pages_found"].setdefault(cls, link["url"])

    targets = _select_targets(links)
    fetched_texts, fetched_html = [parsed["text"]], [html]
    # The homepage always describes the offering, so it always seeds this one.
    offer_texts = [parsed["text"]]

    classed = [t for t in targets if t["classes"]]
    trace.event("crawl", "Crawl queue", "ok",
                f"{len(targets)} of {len(links)} links queued at depth 1, "
                f"{CRAWL_WORKERS} workers, {PER_CALL_TIMEOUT_S}s each · "
                f"{len(classed)} classified: "
                + (", ".join(sorted({c for t in classed for c in t["classes"]})) or "none"))

    if targets and not deadline.expired():
        with ThreadPoolExecutor(max_workers=CRAWL_WORKERS) as pool:
            futures = {
                pool.submit(fetch, t["url"], deadline, PER_CALL_TIMEOUT_S): t
                for t in targets
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

                record = {
                    "url": target["url"],
                    "classes": target["classes"],
                    "ok": bool(result.get("ok")) and result.get("status", 0) < 400,
                    "status": result.get("status"),
                    "error": result.get("error"),
                }
                bundle["crawled"].append(record)
                tag = "/".join(target["classes"]) or "unclassified"
                if not record["ok"]:
                    trace.event("crawl", f"GET {target['url']}", "warn",
                                f"{tag} · {record['error'] or 'HTTP ' + str(record['status'])}")
                    continue

                bundle["links_fetched"] += 1
                sub = parse_html(result.get("body", ""))
                record["word_count"] = sub["word_count"]
                trace.event("crawl", f"GET {target['url']}", "ok",
                            f"{tag} · HTTP {record['status']} · {sub['word_count']:,} words"
                            + ("" if not BOILERPLATE_CLASSES.intersection(target["classes"])
                               else " · boilerplate, withheld from category inference"))
                fetched_texts.append(sub["text"])
                fetched_html.append(result.get("body", ""))
                if not BOILERPLATE_CLASSES.intersection(target["classes"]):
                    offer_texts.append(sub["text"])
                for cls in target["classes"]:
                    existing = bundle["pages"].get(cls)
                    # Prefer the page with real content over a stub redirect.
                    if not existing or sub["word_count"] > existing["word_count"]:
                        bundle["pages"][cls] = {
                            "url": target["url"],
                            "word_count": sub["word_count"],
                            "title": sub["title"],
                        }

    if deadline.expired() and bundle["links_fetched"] < len(targets):
        bundle["crawl_truncated"] = True
        trace.event("crawl", "Crawl truncated", "warn",
                    f"the 8s budget ran out with {len(targets) - bundle['links_fetched']} "
                    "queued pages unfetched")

    bundle["combined_text"] = " ".join(fetched_texts)[:400_000]
    bundle["combined_html"] = " ".join(fetched_html)[:1_200_000]
    bundle["category_text"] = " ".join(offer_texts)[:400_000]

    from .netcalls import emails_in

    bundle["emails"] = emails_in(bundle["combined_text"])[:10]
    trace.event("crawl", "Crawl complete", "ok",
                f"{bundle['links_fetched']}/{len(targets)} pages fetched · "
                f"{len(bundle['combined_text'].split()):,} words of evidence · "
                f"policy pages found: {', '.join(sorted(bundle['pages'])) or 'none'} · "
                f"{len(bundle['emails'])} contact email(s)")
    return bundle
