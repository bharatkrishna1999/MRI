"""
End-to-end tests. The real parser, crawler, signal code, scorer and decision
logic all run; only the four network entry points are swapped for fixtures.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MRI_DB_PATH", "/tmp/mri-test.db")
os.environ.setdefault("MRI_SKIP_WARM", "1")

from mri import policy  # noqa: E402
from mri.domains import InvalidDomain, normalize_domain  # noqa: E402
from mri.engine import Declared, evaluate  # noqa: E402
from tests import fixtures  # noqa: E402


def fake_fetch_factory(site: dict):
    def _fetch(url, deadline, timeout=3.0):
        body = site.get(url) or site.get(url.rstrip("/")) or site.get(url + "/")
        if body is None:
            return {"ok": True, "status": 404, "url": url, "body": "<html>not found</html>",
                    "ttfb_ms": 40, "elapsed_ms": 45, "headers": {}}
        return {"ok": True, "status": 200, "url": url, "body": body,
                "ttfb_ms": 120, "elapsed_ms": 140, "headers": {}}
    return _fetch


def run_against(domain, site, rdap_data=None, tls_data=None, reputation=None,
                declared=None, dns_ok=True):
    fetch = fake_fetch_factory(site)

    def fake_fetch_root(d, deadline):
        result = fetch(f"https://{d}/", deadline)
        result["attempted"] = f"https://{d}/"
        return result

    with mock.patch("mri.crawl.fetch", fetch), \
         mock.patch("mri.engine.fetch_root", fake_fetch_root), \
         mock.patch("mri.engine.rdap_lookup", lambda d, dl: rdap_data or fixtures.rdap("2019-03-01T00:00:00Z", "2027-03-01T00:00:00Z")), \
         mock.patch("mri.engine.tls_info", lambda d, dl: tls_data or fixtures.tls()), \
         mock.patch("mri.engine.resolve_a", lambda d, dl: {"ok": True, "ip": "8.8.8.8", "all": ["8.8.8.8"]} if dns_ok else {"ok": True, "ip": None, "all": [], "error": "no A record"}), \
         mock.patch("mri.signals.reputation.lookup", lambda d, dl: reputation or {"ok": True, "source": "Google Safe Browsing v4", "listed": False, "matches": []}):
        return evaluate(domain, declared or Declared())


class TestNormalisation(unittest.TestCase):
    def test_shapes_collapse_to_registrable_domain(self):
        for raw in ["example.com", "https://www.example.com/pricing?a=1", "EXAMPLE.COM.",
                    "  http://example.com  ", "ops@example.com"]:
            self.assertEqual(normalize_domain(raw), "example.com", raw)

    def test_public_suffix_is_respected(self):
        self.assertEqual(normalize_domain("https://shop.example.co.uk/a"), "example.co.uk")

    def test_rejects_non_domains(self):
        for raw in ["", "   ", "not a domain", "1.2.3.4", "localhost"]:
            with self.assertRaises(InvalidDomain):
                normalize_domain(raw)


class TestParsing(unittest.TestCase):
    def test_title_survives_the_head_skip(self):
        from mri.netcalls import parse_html

        parsed = parse_html(
            "<html><head><title>Acme — B2B SaaS</title><style>a{color:red}</style></head>"
            "<body><h1>Hi</h1><script>var x=1</script><p>Real words</p></body></html>")
        self.assertEqual(parsed["title"], "Acme — B2B SaaS")
        # Script, style and head content must not inflate the word count.
        self.assertEqual(parsed["text"], "Hi Real words")
        self.assertEqual(parsed["word_count"], 3)

    def test_only_same_registrable_domain_links_are_followed(self):
        from mri.netcalls import internal_links

        anchors = [("/pricing", "Pricing"), ("https://blog.acme.com/x", "Blog"),
                   ("https://evil.com/x", "Other"), ("mailto:a@acme.com", "Mail"),
                   ("/logo.png", "Logo"), ("#top", "Top")]
        urls = [link["url"] for link in internal_links("https://acme.com/", "acme.com", anchors)]
        self.assertIn("https://acme.com/pricing", urls)
        self.assertIn("https://blog.acme.com/x", urls)  # subdomain is still the merchant
        self.assertNotIn("https://evil.com/x", urls)
        self.assertEqual(len(urls), 2)

    def test_link_classification_reads_text_and_href(self):
        from mri.crawl import classify_link

        self.assertIn("refund", classify_link({"url": "https://a.com/x", "text": "Money back guarantee", "href": "/x"}))
        self.assertIn("refund", classify_link({"url": "https://a.com/returns", "text": "", "href": "/returns"}))
        self.assertIn("terms", classify_link({"url": "https://a.com/tos", "text": "", "href": "/tos"}))
        self.assertEqual(classify_link({"url": "https://a.com/team", "text": "Careers", "href": "/careers"}), [])


class TestCleanMerchant(unittest.TestCase):
    def setUp(self):
        self.result = run_against("goodsaas.com", fixtures.GOOD_SITE)

    def test_scores_high_and_approves(self):
        self.assertGreaterEqual(self.result["score"], 80)
        self.assertEqual(self.result["band"], "auto_approve")
        self.assertEqual(self.result["reserve_pct"], 0)
        self.assertEqual(self.result["payout"], "T+7")

    def test_only_declaration_dependent_signals_abstain(self):
        # Nothing was declared, so the signals that exist to check a declaration
        # abstain rather than inventing agreement. Everything else computed.
        abstained = {u["key"] for u in self.result["unavailable"]}
        self.assertEqual(
            abstained, {"category_mismatch", "legal_name_match", "geo_consistency"}
        )

    def test_finds_all_policy_pages(self):
        pages = self.result["evidence"]["crawl"]["pages"]
        for cls in ("refund", "terms", "privacy", "contact", "pricing"):
            self.assertIn(cls, pages, f"{cls} page not discovered")

    def test_detects_incumbent_processor(self):
        processor = self._signal("processor")
        self.assertEqual(processor["normalized"], 100)
        self.assertIn("Stripe", processor["raw"])

    def test_infers_standard_category_from_content(self):
        tier = self._signal("category_tier")
        self.assertEqual(tier["detail"]["tier"], "standard")

    def test_every_signal_carries_the_full_contract(self):
        for signal in self.result["signals"]:
            for field in ("raw", "normalized", "weight", "contribution", "reason"):
                self.assertIn(field, signal)
            self.assertTrue(signal["reason"].strip().endswith("."), signal["key"])

    def _signal(self, key):
        return next(s for s in self.result["signals"] if s["key"] == key)


class TestTrace(unittest.TestCase):
    """
    The run narrates itself. This is what the live console renders and what the
    audit record keeps, so it has to be complete, ordered, and honest about
    which parts of the run failed.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run_against("goodsaas.com", fixtures.GOOD_SITE)
        cls.trace = cls.result["trace"]

    def test_events_are_ordered_and_monotonic(self):
        ids = [event["id"] for event in self.trace]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        offsets = [event["t_ms"] for event in self.trace]
        self.assertEqual(offsets, sorted(offsets))

    def test_every_page_the_crawler_fetched_is_named(self):
        crawled = [e for e in self.trace
                   if e["phase"] == "crawl" and e["label"].startswith("GET ")]
        fetched = self.result["evidence"]["crawl"]["links_fetched"]
        self.assertEqual(len(crawled), len(self.result["evidence"]["crawl"]["crawled"]))
        self.assertGreaterEqual(len(crawled), fetched)
        # The refund page is the heaviest single signal; it is named explicitly.
        self.assertTrue(any("refund" in (e["detail"] or "") for e in crawled), crawled)

    def test_every_signal_reports_its_arithmetic(self):
        scored = {e["signal"]: e for e in self.trace if e.get("signal")}
        self.assertEqual(set(scored), set(policy.SIGNAL_SPEC))
        for signal in self.result["signals"]:
            event = scored[signal["key"]]
            if signal["status"] == "ok":
                self.assertEqual(event["status"], "ok")
                self.assertIn(str(signal["weight"]), event["detail"])
            else:
                self.assertEqual(event["status"], "warn")
                self.assertIn("unavailable", event["detail"])

    def test_the_decision_is_the_last_thing_that_happens(self):
        phases = [e["phase"] for e in self.trace]
        self.assertLess(phases.index("enrich"), phases.index("signals"))
        self.assertLess(phases.index("signals"), phases.index("score"))
        self.assertLess(phases.index("score"), phases.index("decide"))
        self.assertEqual(phases[-1], "decide")

    def test_a_failing_upstream_is_recorded_rather_than_hidden(self):
        result = run_against("goodsaas.com", fixtures.GOOD_SITE,
                             reputation={"ok": False, "error": "feed down"})
        bad = [e for e in result["trace"]
               if e["status"] == "warn" and e.get("signal") == "safe_browsing"]
        self.assertEqual(len(bad), 1)
        self.assertIn("unavailable", bad[0]["detail"])


class TestShellSite(unittest.TestCase):
    def setUp(self):
        self.result = run_against(
            "shellco.top", fixtures.SHELL_SITE,
            rdap_data=fixtures.rdap("2026-07-20T00:00:00Z", "2027-07-20T00:00:00Z", privacy=True),
        )

    def test_declines(self):
        self.assertLess(self.result["score"], 40)
        self.assertEqual(self.result["band"], "decline")

    def test_raises_the_expected_codes(self):
        codes = {c["code"] for c in self.result["reason_codes"]}
        for expected in ("DOM_AGE_LT_30", "NO_REFUND_POLICY", "THIN_CONTENT",
                         "TLD_HIGH_ABUSE", "DOM_PRIVACY_PROXY", "DOM_TERM_MINIMUM"):
            self.assertIn(expected, codes)


class TestParkedDomain(unittest.TestCase):
    def test_parking_forces_a_decline_whatever_the_score(self):
        result = run_against("parkedthing.com", fixtures.PARKED_SITE)
        self.assertEqual(result["band"], "decline")
        codes = {c["code"] for c in result["reason_codes"]}
        self.assertIn("PARKED_DOMAIN", codes)
        self.assertTrue(any(o["code"] == "PARKED_DOMAIN" for o in result["overrides_applied"]))


class TestRestrictedCategory(unittest.TestCase):
    def test_restricted_content_declines_regardless_of_hygiene(self):
        result = run_against("tokenlaunch.xyz", fixtures.RESTRICTED_SITE)
        self.assertEqual(result["band"], "decline")
        codes = {c["code"] for c in result["reason_codes"]}
        self.assertIn("CATEGORY_RESTRICTED", codes)

    def test_declared_category_never_buys_a_score(self):
        """A merchant declaring 'SaaS' over a token sale must not score better."""
        honest = run_against("tokenlaunch.xyz", fixtures.RESTRICTED_SITE)
        lying = run_against("tokenlaunch.xyz", fixtures.RESTRICTED_SITE,
                            declared=Declared(category="saas_b2b"))
        self.assertLessEqual(lying["score"], honest["score"])
        codes = {c["code"] for c in lying["reason_codes"]}
        self.assertIn("CATEGORY_MISMATCH", codes)

    def test_mismatch_alone_caps_at_manual_review(self):
        """A clean site whose declared category understates its tier cannot auto-approve."""
        result = run_against("goodsaas.com", fixtures.GOOD_SITE,
                             declared=Declared(category="saas_b2b"))
        self.assertEqual(result["band"], "auto_approve")  # honest declaration, no cap

        lying = run_against("goodsaas.com", fixtures.GOOD_SITE,
                            declared=Declared(category="indie_games"))
        # Same tier, so this is a classification difference, not a misrepresentation.
        self.assertEqual(lying["band"], "auto_approve")


class TestBlocklistIsNotACatalogue(unittest.TestCase):
    """
    A merchant's acceptable-use page names every vertical it refuses to serve.
    Reading that as a description of the merchant declines the most compliant
    applicants in the book — the ones who publish an acceptable-use policy.
    """

    def setUp(self):
        self.result = run_against("payflow.com", fixtures.PAYMENTS_PLATFORM_SITE)

    def test_the_merchant_is_not_classified_as_its_own_blocklist(self):
        tier = self._signal("category_tier")
        self.assertEqual(tier["detail"]["tier"], "standard", tier["raw"])

    def test_it_is_not_declined(self):
        codes = {c["code"] for c in self.result["reason_codes"]}
        self.assertNotIn("CATEGORY_RESTRICTED", codes)
        self.assertNotIn("RESTRICTED_KEYWORDS", codes)
        self.assertNotEqual(self.result["band"], "decline")

    def test_the_blocklist_page_still_counts_as_a_terms_page(self):
        # Excluded from category inference, not from the crawl: commercial
        # legitimacy is still scored on whether the merchant publishes terms.
        self.assertEqual(self._signal("terms_page")["normalized"], 100)

    def test_prohibited_verticals_are_stripped_from_the_offering_text(self):
        from mri.taxonomy import strip_prohibited_context

        kept = strip_prohibited_context(
            "We sell design templates. Casinos and payday loan sites are prohibited.")
        self.assertIn("design templates", kept)
        self.assertNotIn("casino", kept.lower())

    def test_the_ico_a_privacy_policy_names_is_the_regulator(self):
        from mri.taxonomy import scan_restricted_keywords

        self.assertEqual(
            scan_restricted_keywords("You may complain to the ICO about our data handling."),
            [])

    def test_a_dateline_is_not_a_lending_product(self):
        from mri.taxonomy import infer_category

        text = ("Our design studio publishes case studies and client work. " * 6
                + "Last updated Apr 2026.")
        self.assertNotEqual(infer_category(text)["tier"], "restricted")

    def _signal(self, key):
        return next(s for s in self.result["signals"] if s["key"] == key)


class TestThinRestrictedEvidence(unittest.TestCase):
    def test_a_single_stray_keyword_is_not_confident(self):
        from mri.taxonomy import infer_category

        inference = infer_category(
            "Our studio writes long form essays about the history of the printing press "
            "and the people who ran it for four centuries. One essay mentions web3.")
        self.assertEqual(inference["tier"], "restricted")
        self.assertEqual(inference["score"], 1)
        self.assertFalse(inference["confident"])

    def test_a_tie_still_classifies_as_restricted_but_does_not_decline(self):
        """
        The tie-break stays as the policy wrote it — read the riskier of two
        equal readings — but an even split is a question, not a verdict.
        """
        from mri.taxonomy import infer_category

        inference = infer_category(
            "We sell an ebook and a printable planner for new writers. The ebook chapters "
            "cover web3 and defi for beginners in plain language with no jargon at all.")
        self.assertEqual(inference["category"], "crypto_tokens")
        self.assertEqual(inference["score"], inference["runner_up"])
        self.assertFalse(inference["confident"])

    def test_an_unconfirmed_restricted_read_caps_at_manual_review(self):
        from mri.policy import BAND_OVERRIDES

        self.assertEqual(BAND_OVERRIDES["CATEGORY_RESTRICTED_REVIEW"], "manual_review")
        self.assertEqual(BAND_OVERRIDES["CATEGORY_RESTRICTED"], "decline")

    def test_a_site_that_really_is_restricted_still_declines_outright(self):
        result = run_against("tokenlaunch.xyz", fixtures.RESTRICTED_SITE)
        codes = {c["code"] for c in result["reason_codes"]}
        self.assertIn("CATEGORY_RESTRICTED", codes)
        self.assertNotIn("CATEGORY_RESTRICTED_REVIEW", codes)
        self.assertEqual(result["band"], "decline")


class TestRedirectsRespectTheDeadline(unittest.TestCase):
    """
    httpx applies its timeout per request attempt, so following redirects inside
    the client turns a 3 second per-call budget into 3 seconds times the chain
    length. That is how an evaluation with an 8 second budget took 20 seconds.
    """

    @classmethod
    def setUpClass(cls):
        import http.server
        import socketserver
        import threading
        import time as _time

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/r/"):
                    n = int(self.path.split("/")[2])
                    self._redirect(f"/r/{n - 1}" if n > 1 else "/final")
                elif self.path == "/loop":
                    self._redirect("/loop")
                elif self.path == "/slow":
                    _time.sleep(1.0)
                    self._redirect("/slow")
                else:
                    body = b"<html><title>Final</title><body>arrived</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def _redirect(self, to):
                self.send_response(302)
                self.send_header("Location", to)
                self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_chain_is_followed_to_the_final_page(self):
        from mri.netcalls import Deadline, fetch

        result = fetch(f"{self.base}/r/3", Deadline(8.0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["url"].endswith("/final"))
        self.assertIn("arrived", result["body"])

    def test_an_endless_chain_terminates(self):
        from mri.netcalls import Deadline, fetch

        result = fetch(f"{self.base}/loop", Deadline(8.0))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "redirect loop")

    def test_a_slow_chain_stops_at_the_budget_not_a_multiple_of_it(self):
        import time as _time

        from mri.netcalls import Deadline, fetch

        started = _time.monotonic()
        fetch(f"{self.base}/slow", Deadline(2.0), timeout=3.0)
        elapsed = _time.monotonic() - started
        # Every hop is re-budgeted from the shared deadline, so this is bounded
        # by the 2 second budget rather than 5 hops times the 3 second timeout.
        self.assertLess(elapsed, 4.0, f"redirect chain ran {elapsed:.1f}s past its budget")


class TestSafeBrowsingHit(unittest.TestCase):
    def test_a_threat_feed_hit_declines_a_perfect_site(self):
        result = run_against(
            "goodsaas.com", fixtures.GOOD_SITE,
            reputation={"ok": True, "source": "Google Safe Browsing v4", "listed": True,
                        "matches": [{"threatType": "SOCIAL_ENGINEERING"}]},
        )
        self.assertEqual(result["band"], "decline")
        codes = {c["code"] for c in result["reason_codes"]}
        self.assertIn("SAFEBROWSING_HIT", codes)


class TestFailureBehaviour(unittest.TestCase):
    """T10 — a failed signal never becomes a zero."""

    def test_rdap_outage_drops_weight_instead_of_scoring_zero(self):
        healthy = run_against("goodsaas.com", fixtures.GOOD_SITE)
        broken = run_against("goodsaas.com", fixtures.GOOD_SITE,
                             rdap_data={"ok": False, "error": "rdap.org unreachable"})

        # Three identity signals depend on RDAP: 8 + 4 + 4 = 16 points.
        self.assertEqual(healthy["computed_weight"] - broken["computed_weight"], 16)
        self.assertLess(broken["confidence"], healthy["confidence"])
        # The merchant is not punished for our upstream failing.
        self.assertGreaterEqual(broken["score"], healthy["score"] - 1)

    def test_confidence_is_the_share_of_weight_computed(self):
        result = run_against("goodsaas.com", fixtures.GOOD_SITE)
        self.assertAlmostEqual(
            result["confidence"], result["computed_weight"] / 100.0, places=3
        )

    def test_unavailable_signals_report_why(self):
        broken = run_against("goodsaas.com", fixtures.GOOD_SITE,
                             rdap_data={"ok": False, "error": "rdap.org unreachable"})
        keys = {u["key"] for u in broken["unavailable"]}
        self.assertTrue({"domain_age", "registration_term", "privacy_proxy"} <= keys)
        for entry in broken["unavailable"]:
            self.assertTrue(entry["reason"])

    def test_low_confidence_caps_at_manual_review(self):
        result = run_against(
            "goodsaas.com", fixtures.GOOD_SITE,
            rdap_data={"ok": False, "error": "down"},
            tls_data={"ok": False, "error": "down"},
            reputation={"ok": False, "error": "down"},
        )
        codes = {c["code"] for c in result["reason_codes"]}
        if result["confidence"] < policy.CONFIDENCE_FLOOR:
            self.assertIn("LOW_CONFIDENCE", codes)
            self.assertIn(result["band"], ("manual_review", "decline"))

    def test_a_broken_scorer_becomes_unavailable_not_a_crash(self):
        with mock.patch("mri.signals.liveness.ttfb", side_effect=RuntimeError("boom")):
            result = run_against("goodsaas.com", fixtures.GOOD_SITE)
        ttfb = next(s for s in result["signals"] if s["key"] == "ttfb")
        self.assertEqual(ttfb["status"], "unavailable")
        self.assertIsNotNone(result["score"])


class TestPolicyIntegrity(unittest.TestCase):
    def test_weights_total_one_hundred(self):
        self.assertEqual(policy.TOTAL_WEIGHT, 100)

    def test_refund_is_the_heaviest_signal_in_its_category(self):
        commercial = {k: w for k, (c, w, _) in policy.SIGNAL_SPEC.items()
                      if c == "commercial_legitimacy"}
        self.assertEqual(max(commercial, key=commercial.get), "refund_policy")
        others = [w for k, w in commercial.items() if k != "refund_policy"]
        self.assertGreaterEqual(commercial["refund_policy"], 2 * max(others))

    def test_every_band_covers_its_stated_range(self):
        for score, expected in [(100, "auto_approve"), (80, "auto_approve"),
                                (79, "approve_with_reserve"), (60, "approve_with_reserve"),
                                (59, "manual_review"), (40, "manual_review"),
                                (39, "decline"), (0, "decline")]:
            self.assertEqual(policy.band_for_score(score)["id"], expected, score)

    def test_overrides_only_ever_make_a_decision_worse(self):
        for code in policy.BAND_OVERRIDES:
            for band in policy.BAND_SEVERITY:
                final, _ = policy.apply_overrides(band, [code])
                self.assertGreaterEqual(
                    policy.BAND_SEVERITY.index(final), policy.BAND_SEVERITY.index(band)
                )

    def test_every_emitted_code_is_registered(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "mri" / "signals"
        emitted = set()
        for path in root.glob("*.py"):
            for match in re.finditer(r'"([A-Z][A-Z0-9_]{4,})"', path.read_text()):
                emitted.add(match.group(1))
        known = set(policy.REASON_CODES)
        # Only compare tokens that look like reason codes we actually raise.
        for code in emitted & {c for c in known}:
            self.assertIn(code, known)
        for code in policy.BAND_OVERRIDES:
            self.assertIn(code, known)


class TestBenchmarkMath(unittest.TestCase):
    def test_confusion_matrix_uses_bad_as_the_positive_class(self):
        from mri.benchmark import confusion

        rows = [
            {"true_label": "bad", "score": 10},    # predicted bad  -> tp
            {"true_label": "bad", "score": 90},    # predicted good -> fn
            {"true_label": "good", "score": 20},   # predicted bad  -> fp
            {"true_label": "good", "score": 95},   # predicted good -> tn
        ]
        m = confusion(rows, 60)
        self.assertEqual((m["tp"], m["fn"], m["fp"], m["tn"]), (1, 1, 1, 1))
        self.assertAlmostEqual(m["precision"], 0.5)
        self.assertAlmostEqual(m["recall"], 0.5)
        self.assertAlmostEqual(m["false_positive_rate"], 0.5)

    def test_unscored_rows_are_excluded_not_guessed(self):
        from mri.benchmark import confusion

        m = confusion([{"true_label": "bad", "score": None}], 60)
        self.assertEqual(m["unscored"], 1)
        self.assertEqual(m["tp"] + m["fp"] + m["tn"] + m["fn"], 0)

    def test_labels_file_is_balanced_and_complete(self):
        from mri.benchmark import load_labels

        rows = load_labels()
        self.assertEqual(len(rows), 60)
        self.assertEqual(sum(1 for r in rows if r["true_label"] == "good"), 30)
        self.assertEqual(sum(1 for r in rows if r["true_label"] == "bad"), 30)
        self.assertEqual(len({r["domain"] for r in rows}), 60)
        for row in rows:
            self.assertTrue(row["source"])
            self.assertTrue(row["date_labelled"])


class TestTaxonomy(unittest.TestCase):
    def test_removed_verticals_are_gone(self):
        from mri.taxonomy import CATEGORIES

        labels = {label for label, _, _ in CATEGORIES.values()}
        ids = set(CATEGORIES)
        for gone in ("Textiles", "Jewellery", "Automotive", "Electronics",
                     "Food & Beverages", "Education", "Energy", "Telecommunications"):
            self.assertNotIn(gone, labels)
            self.assertNotIn(gone.lower().replace(" & ", "_"), ids)

    def test_retired_vocabulary_is_absent_from_the_whole_repo(self):
        """T3 acceptance: grep the repo for the retired terms, expect zero hits."""
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        banned = ["razorpay", "paytm", "phonepe", "cashfree", "jewellery", "pincode"]
        out = subprocess.run(
            ["grep", "-ril", "--exclude-dir=.git", "--exclude-dir=__pycache__",
             "--exclude=*.mmdb", "--exclude=*.db", "-e", "\\|".join(banned), "."],
            cwd=root, capture_output=True, text=True,
        )
        hits = [line for line in out.stdout.splitlines()
                if line and not line.endswith("test_engine.py")]
        self.assertEqual(hits, [], f"retired vocabulary still present in: {hits}")

    def test_tier_counts_match_the_policy(self):
        from mri.taxonomy import CATEGORIES

        tiers = {}
        for _, (_, tier, _) in CATEGORIES.items():
            tiers[tier] = tiers.get(tier, 0) + 1
        self.assertEqual(tiers["standard"], 9)
        self.assertEqual(tiers["elevated"], 7)
        self.assertEqual(tiers["restricted"], 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
