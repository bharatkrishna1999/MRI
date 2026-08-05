"""HTTP surface tests, including the audit trail and the documented curl."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MRI_DB_PATH", "/tmp/mri-api-test.db")
os.environ["MRI_SKIP_WARM"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from mri.api import app  # noqa: E402
from mri.policy import POLICY_VERSION  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.test_engine import fake_fetch_factory  # noqa: E402

client = TestClient(app)


def patched(site=fixtures.GOOD_SITE, domain="goodsaas.com"):
    fetch = fake_fetch_factory(site)

    def fake_fetch_root(d, deadline):
        result = fetch(f"https://{d}/", deadline)
        result["attempted"] = f"https://{d}/"
        return result

    return [
        mock.patch("mri.crawl.fetch", fetch),
        mock.patch("mri.engine.fetch_root", fake_fetch_root),
        mock.patch("mri.engine.rdap_lookup",
                   lambda d, dl: fixtures.rdap("2019-03-01T00:00:00Z", "2027-03-01T00:00:00Z")),
        mock.patch("mri.engine.tls_info", lambda d, dl: fixtures.tls()),
        mock.patch("mri.engine.resolve_a", lambda d, dl: {"ok": True, "ip": "8.8.8.8", "all": ["8.8.8.8"]}),
        mock.patch("mri.signals.reputation.lookup",
                   lambda d, dl: {"ok": True, "source": "Google Safe Browsing v4",
                                  "listed": False, "matches": []}),
    ]


class _Patched:
    def __init__(self, *a, **kw):
        self.patches = patched(*a, **kw)

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class TestPages(unittest.TestCase):
    def test_index_is_a_single_field_form(self):
        html = client.get("/").text
        self.assertIn('id="domain"', html)
        self.assertIn("Advanced", html)
        # Every other input lives inside the collapsed Advanced panel.
        before_advanced = html.split("<details", 1)[0]
        self.assertEqual(before_advanced.count('<input id='), 1)

    def test_benchmark_page_renders(self):
        self.assertEqual(client.get("/benchmark").status_code, 200)


class TestPolicyEndpoint(unittest.TestCase):
    def test_exposes_the_versioned_policy(self):
        body = client.get("/api/v1/policy").json()
        self.assertEqual(body["policy_version"], POLICY_VERSION)
        self.assertEqual(sum(body["category_weights"].values()), 100)
        self.assertEqual(len(body["signals"]), 24)
        self.assertEqual(len(body["bands"]), 4)
        self.assertEqual(len(body["taxonomy"]), 25)

    def test_bands_carry_reserve_and_payout(self):
        bands = {b["id"]: b for b in client.get("/api/v1/policy").json()["bands"]}
        self.assertEqual(bands["auto_approve"]["reserve_pct"], 0)
        self.assertEqual(bands["auto_approve"]["payout"], "T+7")
        self.assertEqual(bands["approve_with_reserve"]["reserve_pct"], 5)
        self.assertEqual(bands["approve_with_reserve"]["reserve_hold_days"], 90)
        self.assertEqual(bands["approve_with_reserve"]["payout"], "T+14")
        self.assertEqual(len(bands["manual_review"]["documents"]), 3)


class TestEvaluateEndpoints(unittest.TestCase):
    def test_get_returns_the_full_structured_decision(self):
        with _Patched():
            body = client.get("/api/v1/evaluate", params={"domain": "https://goodsaas.com/pricing"}).json()
        self.assertEqual(body["domain"], "goodsaas.com")
        for field in ("score", "confidence", "decision", "band", "reserve_pct", "payout",
                      "reason_codes", "categories", "signals", "policy_version", "api"):
            self.assertIn(field, body)
        self.assertIn("curl", body["api"])
        self.assertIn("goodsaas.com", body["api"]["curl"])

    def test_post_accepts_a_bare_domain_and_nothing_else(self):
        with _Patched():
            response = client.post("/api/v1/evaluate", json={"domain": "goodsaas.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["band"], "auto_approve")

    def test_invalid_domain_is_a_400_not_a_verdict(self):
        response = client.post("/api/v1/evaluate", json={"domain": "not a domain"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("resolvable", response.json()["detail"])

    def test_second_call_is_served_from_the_24h_cache(self):
        with _Patched():
            first = client.post("/api/v1/evaluate", json={"domain": "goodsaas.com", "refresh": True}).json()
            second = client.post("/api/v1/evaluate", json={"domain": "goodsaas.com"}).json()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["score"], second["score"])


class TestAuditTrail(unittest.TestCase):
    def test_every_run_is_persisted_and_replayable(self):
        with _Patched():
            result = client.post("/api/v1/evaluate",
                                 json={"domain": "goodsaas.com", "refresh": True}).json()
        audit_id = result["audit_id"]
        self.assertIsNotNone(audit_id)

        replay = client.get(f"/api/v1/audit/{audit_id}").json()
        self.assertEqual(replay["domain"], "goodsaas.com")
        self.assertEqual(replay["score"], result["score"])
        self.assertEqual(replay["policy_version"], POLICY_VERSION)
        # Raw signal values survive, not just the score.
        self.assertEqual(len(replay["signals"]), 24)
        self.assertTrue(any(s["raw"] for s in replay["signals"]))

    def test_recent_lists_runs_with_their_codes(self):
        with _Patched():
            client.post("/api/v1/evaluate", json={"domain": "goodsaas.com", "refresh": True})
        body = client.get("/api/v1/audit/recent").json()
        self.assertGreaterEqual(len(body["runs"]), 1)
        self.assertIn("policy_version", body["runs"][0])
        self.assertIsInstance(body["runs"][0]["reason_codes"], list)

    def test_missing_audit_id_is_a_404(self):
        self.assertEqual(client.get("/api/v1/audit/99999999").status_code, 404)


class TestBenchmarkEndpoints(unittest.TestCase):
    def test_labels_are_served_from_the_committed_csv(self):
        body = client.get("/api/v1/benchmark/labels").json()
        self.assertEqual(body["total"], 60)
        self.assertEqual(body["good"], 30)
        self.assertEqual(body["bad"], 30)
        self.assertEqual(
            set(body["rows"][0]), {"domain", "true_label", "source", "date_labelled"}
        )

    def test_results_endpoint_says_so_when_no_run_exists(self):
        response = client.get("/api/v1/benchmark/results")
        self.assertIn(response.status_code, (200, 404))
        if response.status_code == 404:
            self.assertIn("hint", response.json())


class TestHealth(unittest.TestCase):
    def test_health_reports_the_bundled_geoip_database(self):
        body = client.get("/api/v1/health").json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["geoip"]["available"], body["geoip"])
        self.assertEqual(body["geoip"]["database_type"], "GeoLite2-Country")


if __name__ == "__main__":
    unittest.main(verbosity=2)
