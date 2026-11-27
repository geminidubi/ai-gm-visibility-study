"""The G5 pilot exercises the real SDK with an in-process transport."""

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import OpenAI

from ai_gm_visibility.g5_panel import adopt_attempts, load_panel
from ai_gm_visibility.g5_pilot import prepare_pilot, run_pilot, study_fingerprint
from ai_gm_visibility.llm.g5_ledger import open_g5_ledger
from ai_gm_visibility.logging_io import digest, read_jsonl
from ai_gm_visibility.runner import run
from ai_gm_visibility.schemas import load_json
from test_openai_backend import TEST_KEY, response_body


class G5PilotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ledger = self.root / "g5-sim.sqlite3"
        self.requests = []
        self.handler = None
        http = httpx2.Client(transport=httpx2.MockTransport(self.transport))
        self.client = OpenAI(api_key=TEST_KEY, max_retries=0, timeout=60, http_client=http)
        self.addCleanup(self.client.close)

    def transport(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.handler:
            return self.handler(request)
        reply = response_body(model=body["model"], max_output_tokens=body["max_output_tokens"],
                              reasoning=body["reasoning"])
        reply["usage"]["output_tokens_details"]["reasoning_tokens"] = 5 if body["reasoning"]["effort"] == "low" else 0
        return httpx2.Response(200, json=reply)

    def batch(self, effort, output=None, **options):
        return run_pilot(effort, output or self.root, client=self.client, simulated_ledger=self.ledger, **options)

    def test_preflight_and_whitelist_refuse_before_sdk_construction(self):
        with patch("ai_gm_visibility.g5_pilot.OpenAIModel", side_effect=AssertionError("SDK instantiated")):
            preflight = prepare_pilot()[-1]
            self.assertEqual(len(set(preflight["case_ids"])), 12)
            self.assertEqual([s["requests"] for s in preflight["scenarios"]], [36, 36, 72])
            self.assertLess(preflight["scenarios"][-1]["conservative_quote_usd"], 2)
            with self.assertRaisesRegex(ValueError, "explicit paid authorization"):
                run_pilot("none", self.root)
            with self.assertRaisesRegex(ValueError, "whitelist or prompt changed"):
                with patch("ai_gm_visibility.g5_pilot.FROZEN_INPUTS", {"data/g5/dev_cases.jsonl": "unexpected"}):
                    run_pilot("none", self.root, authorize_paid=True)
            with self.assertRaisesRegex(ValueError, "Formal execution blocked"):
                run(load_json("configs/g5-formal.json"), self.root)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.ledger.exists())

    def test_sdk_dispatch_matches_archived_request_across_efforts_and_batches(self):
        first, result = self.batch("none", batch_size=12)
        self.assertEqual((result["attempts"], result["g5_totals"]["requests_used"]), (12, 12))
        second, result = self.batch("none", self.root / "different-output")
        self.assertEqual((result["attempts"], result["previously_reserved"]), (24, 12))
        low, result = self.batch("low")
        self.assertEqual((result["attempts"], result["g5_totals"]["requests_used"]), (36, 72))
        _, result = self.batch("low")
        self.assertEqual((result["attempts"], result["previously_reserved"]), (0, 36))
        self.assertEqual(result["g5_totals"]["requests_remaining"], 0)
        self.assertEqual(result["g5_comparison"]["paired_successful_units"], 36)
        self.assertEqual(result["g5_comparison"]["efforts"]["low"]["reasoning_tokens"], {"observed": 36, "total": 180})
        for path in (first, second, low):
            _, _, adopted, provenance = load_panel(path)
            self.assertIn("record_seal", provenance)
            for row in read_jsonl(path / "records.jsonl"):
                number = row["g5_request_number"]
                self.assertEqual(row["generation_request"], self.requests[number - 1])
                self.assertEqual(row["generation_request_sha256"], digest(self.requests[number - 1]))
                key = row["case_id"], row["architecture"], row["repeat_id"]
                self.assertEqual(adopted[key]["adopted_record_sha256"], digest(row))
                self.assertEqual(row["reasoning_tokens"], 5 if number > 36 else 0)
            for file in path.iterdir():
                self.assertNotIn(TEST_KEY, file.read_text(encoding="utf-8"))
        self.assertEqual(len(self.requests), 72)
        for none, low in zip(self.requests[:36], self.requests[36:], strict=True):
            self.assertEqual(none | {"reasoning": {"effort": "low"}}, low)

    def test_timeout_and_interruption_remain_reserved_and_are_not_replayed(self):
        def timeout(request):
            raise httpx2.ReadTimeout("contains " + TEST_KEY, request=request)

        self.handler = timeout
        first, result = self.batch("none", batch_size=1)
        record, = read_jsonl(first / "records.jsonl")
        self.assertEqual(record["error_type"], "api_timeout")
        self.assertNotIn(TEST_KEY, (first / "records.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(result["g5_totals"]["attempts_with_unknown_cost"], 1)

        def interrupt(request):
            raise KeyboardInterrupt

        self.handler = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.batch("none", batch_size=1)
        self.handler = None
        _, result = self.batch("none", batch_size=1)
        self.assertEqual(result["previously_reserved"], 2)
        self.assertEqual(result["g5_totals"]["unsettled_requests"], 1)
        self.assertEqual(result["g5_totals"]["attempts_with_unknown_cost"], 2)
        self.assertEqual(len({digest(request) for request in self.requests}), 3)

    def test_budget_limit_and_concurrent_dispatch(self):
        with open_g5_ledger(self.ledger, "test") as ledger:
            quote = Decimal("1.50")
            reservation = self.root / "records.jsonl", "manifest", "request", quote
            self.assertEqual(ledger.reserve("none", "test", "C0", *reservation), (1, None))
            self.assertEqual(ledger.reserve("none", "test", "C1", *reservation),
                             (None, "budget_exhausted"))
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                with open_g5_ledger(self.ledger, "test"):
                    self.fail("Concurrent dispatch opened")

    def test_safety_stops_persist_across_batches(self):
        outcomes = (
            ("auth", 401, {"error": {"message": TEST_KEY}}, "api_http_error", 1),
            ("model", 200, response_body(), "returned_model_mismatch", 1),
            ("limit", 200, response_body(model="gpt-6-luna", max_output_tokens=2048, status="incomplete",
                                        incomplete_details={"reason": "max_output_tokens"}),
             "repeated_output_limit", 3),
            ("system", 503, {"error": {"message": TEST_KEY}}, "consecutive_generation_errors", 3),
        )
        for name, status, body, reason, count in outcomes:
            with self.subTest(name=name):
                self.ledger = self.root / f"{name}.sqlite3"
                self.requests.clear()
                self.handler = lambda _: httpx2.Response(status, json=body)
                first, result = self.batch("none")
                self.assertEqual((result["stop"]["reason"], result["attempts"]), (reason, count))
                self.assertNotIn(TEST_KEY, (first / "records.jsonl").read_text(encoding="utf-8"))
                with self.assertRaisesRegex(ValueError, "prior safety stop"):
                    self.batch("low")
                self.assertEqual(len(self.requests), count)

    def test_resume_refuses_changed_batch_records(self):
        directory, _ = self.batch("none", batch_size=1)
        path = directory / "records.jsonl"
        row, = read_jsonl(path)
        row["response_text"] = "modified"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "batch seal mismatch"):
            self.batch("none")
        self.assertEqual(len(self.requests), 1)
        with self.assertRaisesRegex(ValueError, "Run seal does not match"):
            load_panel(directory)
        self.assertIn("Run seal does not match", read_jsonl(directory / "record_rejections.jsonl")[0]["reason"])

    def connection_error(self, request):
        raise httpx2.ConnectError("proxy connection refused", request=request)

    def authorize_test_recovery(self, initial):
        expected = prepare_pilot()[2]
        fingerprint = study_fingerprint(expected)
        with open_g5_ledger(self.ledger, fingerprint) as ledger:
            before = [dict(row) for row in ledger.rows()]
            summary = ledger.summary()
            self.assertEqual((summary["request_limit"], summary["requests_used"]), (72, 3))
            ledger.authorize_connection_recovery(initial.name, expected, ledger.audit(expected))
            self.assertEqual([dict(row) for row in ledger.rows()], [row | {"replaces_number": None} for row in before])
            self.assertEqual(ledger.summary()["request_limit"], 75)
            self.assertEqual(ledger.summary()["reserved_cost_usd"], summary["reserved_cost_usd"])
            with self.assertRaisesRegex(ValueError, "already been authorized"):
                ledger.authorize_connection_recovery(initial.name, expected, ledger.audit(expected))
        return expected, fingerprint

    def test_connection_recovery_keeps_original_and_completes_all_72_units(self):
        self.handler = self.connection_error
        initial, _ = self.batch("none")
        originals = {p: p.read_bytes() for p in initial.iterdir()}
        expected, fingerprint = self.authorize_test_recovery(initial)
        with open_g5_ledger(self.ledger, fingerprint) as ledger:
            _, case, architecture = next(iter(ledger.audit(expected)))
            with self.assertRaisesRegex(ValueError, "differs from its original reservation"):
                ledger.reserve("none", case, architecture, self.root / "records.jsonl", "manifest",
                               "unapproved payload", Decimal("0.01"))
        self.handler = None
        with self.assertRaisesRegex(ValueError, "36 recovered none units"):
            self.batch("low")
        self.assertEqual(len(self.requests), 3)
        none, none_result = self.batch("none")
        low, low_result = self.batch("low")
        self.assertEqual((none_result["ok"], low_result["ok"]), (36, 36))
        originals_by_number = {row["g5_request_number"]: row for row in read_jsonl(initial / "records.jsonl")}
        resent = [row for row in read_jsonl(none / "records.jsonl") if "g5_replaces_request_number" in row]
        self.assertEqual([row["g5_replaces_request_number"] for row in resent], [1, 2, 3])
        for row in resent:
            original = originals_by_number[row["g5_replaces_request_number"]]
            self.assertEqual(row["generation_request_sha256"], original["generation_request_sha256"])
            self.assertEqual(row["generation_request"], original["generation_request"])
        self.assertEqual(low_result["g5_comparison"]["paired_successful_units"], 36)
        self.assertEqual(low_result["g5_totals"]["requests_used"], 75)
        self.assertEqual(low_result["g5_totals"]["attempts_with_unknown_cost"], 3)
        self.assertEqual(load_panel(none)[0]["config"]["request_limit"], 75)
        self.assertEqual(load_panel(low)[0]["config"]["openai"]["max_retries"], 0)
        self.assertEqual({p: p.read_bytes() for p in initial.iterdir()}, originals)
        with open_g5_ledger(self.ledger, fingerprint) as ledger:
            self.assertEqual(len(ledger.audit(expected)), 72)
            self.assertEqual(ledger.reserve("none", "extra", "C0", self.root / "records.jsonl",
                                            "manifest", "request", Decimal("0.01")), (None, "request_limit_reached"))
        self.assertEqual(len(self.requests), 75)

    def test_connection_recovery_only_replaces_three_original_errors_and_stops_again(self):
        self.handler = self.connection_error
        initial, _ = self.batch("none")
        self.authorize_test_recovery(initial)
        _, first = self.batch("none", batch_size=1)
        self.assertIsNone(first["stop"])
        _, later = self.batch("none")
        self.assertEqual(later["attempts"], 2)
        self.assertEqual(later["stop"]["reason"], "consecutive_generation_errors")
        self.assertEqual(len(self.requests), 6)
        with self.assertRaisesRegex(ValueError, "prior safety stop"):
            self.batch("none")
        with self.assertRaisesRegex(ValueError, "prior safety stop"):
            self.batch("low")

    def test_recovery_refuses_non_connection_stop(self):
        self.handler = lambda _: httpx2.Response(503, json={"error": {"message": "unavailable"}})
        initial, _ = self.batch("none")
        expected = prepare_pilot()[2]
        with open_g5_ledger(self.ledger, study_fingerprint(expected)) as ledger:
            with self.assertRaisesRegex(ValueError, "original three connection errors"):
                ledger.authorize_connection_recovery(initial.name, expected, ledger.audit(expected))
            self.assertEqual(ledger.summary()["request_limit"], 72)

    def test_adoption_rejects_selected_length_sources_and_request_tampering(self):
        config = load_json("configs/g5-dev.json")
        directory, _ = run(config, self.root)
        manifest, cases, _, _ = load_panel(directory)
        rows = read_jsonl(directory / "records.jsonl")
        c2 = next(row for row in rows if row["architecture"] == "C2" and len(row["input_source_fact_ids"]) == 9)
        c1 = next(row for row in rows if row["architecture"] == "C1" and row["history_sources"])
        invalid_payload = c2["generation_request"] | {"messages": c2["rendered_messages"][:-1]}
        attempts = (
            (c2 | {"selected_fact_ids": []}, "selected_fact_ids"),
            (c2 | {"candidate_fact_ids": c2["candidate_fact_ids"][:-1]}, "candidate_fact_ids"),
            (c2 | {"input_source_fact_ids": c2["input_source_fact_ids"][:-1]}, "input_source_fact_ids"),
            (c2 | {"ranked_facts": c2["ranked_facts"][:-1]}, "ranked_facts"),
            (c2 | {"rendered_messages": c2["rendered_messages"][:-1]}, "rendered_messages"),
            (c1 | {"history_sources": []}, "history_sources"),
            ({k: v for k, v in c1.items() if k != "history_ids"}, "history_ids"),
            (c2 | {"generation_request": invalid_payload, "generation_request_sha256": digest(invalid_payload)},
             "generation_request"),
        )
        for row, field in attempts:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"records.jsonl row 1: .*{field}"):
                adopt_attempts(manifest["planned_units"], [row], manifest["run_id"], 1, True,
                               config=config, cases=cases)
