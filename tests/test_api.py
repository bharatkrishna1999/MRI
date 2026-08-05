"""HTTP surface tests, including the audit trail and the documented curl."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MRI_DB_PATH", "/tmp/mri-api-test.db")
os.environ["MRI_SKIP_WARM"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from mri.api import app  # noqa: E402
from mri.policy import POLICY_VERSION, SIGNAL_SPEC  # noqa: E402
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


class TestEvaluationStream(unittest.TestCase):
    """The SSE surface behind the live console."""

    def _drain(self, url):
        events = []
        with client.stream("GET", url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            name = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: ") and name:
                    events.append((name, json.loads(line[6:])))
        return events

    def test_the_run_is_narrated_then_the_decision_arrives(self):
        with _Patched():
            events = self._drain(
                "/api/v1/evaluate/stream?domain=goodsaas.com&refresh=true")

        names = [name for name, _ in events]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-1], "done")
        self.assertEqual(names.count("result"), 1)

        trace = [payload for name, payload in events if name == "trace"]
        self.assertGreater(len(trace), 20, "a full run is more than twenty steps")
        for event in trace:
            self.assertLessEqual({"id", "t_ms", "phase", "label", "status"}, set(event))

        phases = {event["phase"] for event in trace}
        self.assertTrue({"enrich", "signals", "score", "decide"} <= phases, phases)

        # Every signal in the policy reports itself, computed or not.
        scored = {event["signal"] for event in trace if event.get("signal")}
        self.assertEqual(len(scored), len(SIGNAL_SPEC))

        # The decision on the stream is the decision the JSON API would return.
        result = next(payload for name, payload in events if name == "result")
        self.assertEqual(result["domain"], "goodsaas.com")
        self.assertIn("decision", result)
        self.assertIn("curl", result["api"])

    def test_running_steps_are_closed_by_a_terminal_event(self):
        with _Patched():
            events = self._drain(
                "/api/v1/evaluate/stream?domain=goodsaas.com&refresh=true")
        trace = [payload for name, payload in events if name == "trace"]
        opened = {event["id"] for event in trace if event["status"] == "running"}
        closed = {event["ref"] for event in trace if event.get("ref")}
        self.assertEqual(opened, closed, "every running line must be resolved")

    def test_a_non_domain_fails_on_the_stream_rather_than_hanging(self):
        events = self._drain("/api/v1/evaluate/stream?domain=not%20a%20domain%20!!")
        names = [name for name, _ in events]
        self.assertIn("failed", names)
        self.assertNotIn("result", names)
        failure = next(payload for name, payload in events if name == "failed")
        self.assertEqual(failure["status"], 400)

    def test_event_names_do_not_collide_with_eventsource_builtins(self):
        # EventSource dispatches its own `open` and `error`; a server event of
        # either name would be indistinguishable from a transport failure.
        with _Patched():
            events = self._drain(
                "/api/v1/evaluate/stream?domain=goodsaas.com&refresh=true")
        names = {name for name, _ in events}
        self.assertNotIn("open", names)
        self.assertNotIn("error", names)


class TestTraceInTheJson(unittest.TestCase):
    def test_the_plain_json_carries_the_same_trace(self):
        with _Patched():
            body = client.get(
                "/api/v1/evaluate?domain=goodsaas.com&refresh=true").json()
        self.assertGreater(len(body["trace"]), 20)
        self.assertEqual(body["trace"][0]["id"], 1)

    def test_a_cache_hit_keeps_the_two_timelines_apart(self):
        with _Patched():
            client.get("/api/v1/evaluate?domain=cachedsaas.com&refresh=true")
            body = client.get("/api/v1/evaluate?domain=cachedsaas.com").json()
        self.assertTrue(body["cached"])
        # This request did almost nothing, and says so.
        self.assertEqual([e["phase"] for e in body["trace"]], ["cache", "cache"])
        self.assertIn("hit", body["trace"][-1]["detail"])
        # The run that actually made the calls is kept, separately.
        self.assertGreater(len(body["trace_of_cached_run"]), 20)

    def test_the_stored_audit_record_replays_with_its_trace(self):
        with _Patched():
            body = client.get(
                "/api/v1/evaluate?domain=auditedsaas.com&refresh=true").json()
        replay = client.get(f"/api/v1/audit/{body['audit_id']}").json()
        self.assertGreater(len(replay["trace"]), 20)


class TestHealth(unittest.TestCase):
    def test_health_reports_the_bundled_geoip_database(self):
        body = client.get("/api/v1/health").json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["geoip"]["available"], body["geoip"])
        self.assertEqual(body["geoip"]["database_type"], "GeoLite2-Country")


if __name__ == "__main__":
    unittest.main(verbosity=2)
