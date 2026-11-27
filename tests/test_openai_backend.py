"""Exercise the locked SDK through an in-process HTTP transport only."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import OpenAI

from ai_gm_visibility.cli import main
from ai_gm_visibility.llm.openai_backend import OpenAIBudget, OpenAIModel
from ai_gm_visibility.llm.openai_ledger import OpenAILedger
from ai_gm_visibility.logging_io import read_jsonl
from ai_gm_visibility.runner import run, validate_config
from ai_gm_visibility.schemas import ValidationError, load_json


TEST_KEY = "offline-test-key-never-a-real-credential"
ANSWER = "這是離線測試回答。"


def response_body(**overrides):
    return {
        "id": "resp_offline", "object": "response", "created_at": 1,
        "status": "completed", "model": "gpt-6-luna-test-version",
        "max_output_tokens": 1024, "reasoning": {"effort": "none"},
        "store": False, "service_tier": "default", "temperature": 1.0, "top_p": 1.0,
        "prompt_cache_options": {"mode": "explicit"},
        "output": [
            {"type": "message", "id": "msg_offline", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": ANSWER, "annotations": []}]},
        ],
        "usage": {
            "input_tokens": 100, "output_tokens": 25, "total_tokens": 125,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
        **overrides,
    }


class OpenAIBackendTests(unittest.TestCase):
    def setUp(self):
        self.config = load_json("configs/dev-openai.json")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output_root = Path(temp.name)
        self.requests = []

    def batch(self, handler=None, *, ledger=":memory:", output_root=None, **changes):
        config = dict(self.config, **changes)

        def transport(request):
            self.requests.append((request, json.loads(request.content)))
            if handler:
                return handler(request)
            return httpx2.Response(200, json=response_body(), headers={"x-request-id": "req_offline"})

        http = httpx2.Client(transport=httpx2.MockTransport(transport))
        with OpenAI(api_key=TEST_KEY, base_url="https://api.openai.com/v1", timeout=60, max_retries=0, http_client=http) as client:
            directory, counts = run(
                config, output_root or self.output_root, backend=OpenAIModel(config["openai"], client=client),
                simulated_ledger=ledger,
            )
        return directory, counts, read_jsonl(directory / "records.jsonl")

    def test_full_batch_serialization_boundaries_and_comparison(self):
        directory, counts, records = self.batch()
        self.assertEqual((counts["ok"], counts["failed"], counts["attempts"], counts["unattempted"]), (24, 0, 24, 0))
        self.assertEqual(len(self.requests), 24)
        secret = load_json(self.config["world_path"])["facts"][2]["content"]
        events = read_jsonl(directory / "events.jsonl")
        for turn_id, revision in enumerate([0, 0, 1, 1, 1, 1, 2, 2], 1):
            turn = [row for row in events if row["case_id"] == f"DEV_T{turn_id:02d}"]
            self.assertEqual(len(turn), 3)
            for row in turn:
                self.assertEqual(row["state_revision"], revision)
                self.assertEqual(row["after"], turn[0]["after"])
        for (request, body), row in zip(self.requests, records, strict=True):
            self.assertEqual((request.method, request.url.path), ("POST", "/v1/responses"))
            self.assertEqual(request.headers["Authorization"], f"Bearer {TEST_KEY}")
            self.assertEqual(body, {
                "model": "gpt-6-luna", "input": row["rendered_messages"],
                "reasoning": {"effort": "none"}, "max_output_tokens": 1024, "store": False,
                "service_tier": "default", "prompt_cache_options": {"mode": "explicit"},
                "truncation": "disabled",
            })
            self.assertEqual([message["role"] for message in body["input"]], ["system", "system", "system", "user"])
            self.assertNotIn(ANSWER, request.content.decode())
            if row["architecture"] == "C2":
                self.assertEqual(secret in request.content.decode(), row["turn_id"] in {3, 4, 7, 8})
            else:
                self.assertIn(secret, request.content.decode())
            self.assertTrue(row["synthetic"])
            self.assertTrue(row["response_metadata"]["synthetic"])
            self.assertEqual(row["response_metadata"]["request_id"], "req_offline")
        report = (directory / "comparison.md").read_text(encoding="utf-8")
        self.assertTrue(report.startswith("# 離線模擬"))
        self.assertEqual(report.count(ANSWER), 24)
        for path in directory.iterdir():
            self.assertNotIn(TEST_KEY, path.read_text(encoding="utf-8"))

    def test_usage_cost_model_and_effective_settings(self):
        directory, counts, records = self.batch()
        manifest = load_json(directory / "run_manifest.json")
        self.assertEqual(manifest["requested_model"], "gpt-6-luna")
        self.assertEqual(manifest["client_options"], {"timeout": 60, "max_retries": 0})
        self.assertEqual(counts["returned_models"], ["gpt-6-luna-test-version"])
        first = records[0]
        self.assertEqual((first["input_tokens"], first["output_tokens"], first["reasoning_tokens"]), (100, 25, 5))
        self.assertEqual(first["estimated_cost"], 0.0000225)
        self.assertGreater(first["preflight_estimate_usd"], first["estimated_cost"])
        self.assertEqual(first["provider_parameters"]["temperature"], 1.0)
        self.assertEqual(first["parameter_effective_values"]["reasoning"], {"effort": "none"})
        self.assertGreaterEqual(first["latency_ms"], 0)
        self.assertEqual(counts["estimated_cost_usd"], 0.00054)
        self.assertEqual(counts["observed_tokens"]["output_tokens"], 600)
        self.assertEqual(counts["reserved_cost_usd"], 0)

    def test_budget_blocks_before_first_request(self):
        directory, counts, records = self.batch(budget_usd=0.000001)
        self.assertFalse(self.requests)
        self.assertFalse(records)
        self.assertEqual(counts["unattempted"], 24)
        self.assertEqual(counts["stop"]["reason"], "budget_exhausted")
        self.assertEqual(counts["stop"]["case_id"], "DEV_T01")
        self.assertIn("未執行", (directory / "comparison.md").read_text(encoding="utf-8"))

    def test_running_budget_checks_next_request(self):
        request_budget = OpenAIBudget(5, 1024)
        self.assertIsNone(request_budget.quote([{"role": "user", "content": "a" * 272_000}]))
        _, _, normal = self.batch()
        self.requests.clear()
        limit = normal[0]["preflight_estimate_usd"]
        _, counts, records = self.batch(budget_usd=limit)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(counts["stop"]["reason"], "budget_exhausted")
        self.assertEqual(counts["unattempted"], 23)

    def test_timeout_retains_reservation_without_retry(self):
        def timeout(request):
            raise httpx2.ReadTimeout("sensitive error text " + TEST_KEY, request=request)

        _, counts, records = self.batch(timeout, budget_usd=0.0008)
        self.assertEqual(len(self.requests), 1)
        row = records[0]
        self.assertEqual(row["error_type"], "api_timeout")
        self.assertIsNone(row["estimated_cost"])
        self.assertEqual(counts["attempts_with_unknown_cost"], 1)
        self.assertEqual(counts["reserved_cost_usd"], row["preflight_estimate_usd"])
        self.assertEqual(counts["stop"]["reason"], "budget_exhausted")
        self.assertNotIn(TEST_KEY, json.dumps(row))

    def test_http_error_stops_and_does_not_save_body(self):
        def denied(_request):
            return httpx2.Response(401, json={"error": {"message": "invalid key " + TEST_KEY}},
                                   headers={"x-request-id": "req_denied"})

        _, counts, records = self.batch(denied)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(counts["stop"]["reason"], "api_http_error")
        metadata = records[0]["response_metadata"]
        self.assertEqual((metadata["http_status"], metadata["request_id"]), (401, "req_denied"))
        self.assertNotIn(TEST_KEY, json.dumps(records[0]))

    def test_incomplete_refusal_empty_and_malformed_responses(self):
        refusal = response_body()["output"]
        refusal[0]["content"] = [{"type": "refusal", "refusal": "模型拒答。"}]
        variants = [
            (response_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
             "incomplete_response", ANSWER),
            (response_body(output=refusal), "model_refusal", "模型拒答。"),
            (response_body(output=[]), "empty_response", ""),
            (response_body(output=[{"type": "unexpected"}]), "malformed_response", ""),
        ]
        for body, error, answer in variants:
            with self.subTest(error=error):
                _, counts, records = self.batch(lambda _: httpx2.Response(200, json=body), budget_usd=0.0008)
                row = records[0]
                self.assertGreater(counts["failed"], 0)
                self.assertEqual((row["status"], row["error_type"], row["response_text"]), ("error", error, answer))
                self.assertEqual(row["estimated_cost"], 0.0000225)
                if error == "incomplete_response":
                    self.assertEqual(row["response_metadata"]["incomplete_details"]["reason"], "max_output_tokens")

    def test_missing_usage_preserves_answer_and_reserves_cost(self):
        _, counts, records = self.batch(lambda _: httpx2.Response(200, json=response_body(usage=None)), budget_usd=0.0008)
        row = records[0]
        self.assertEqual((row["status"], row["error_type"], row["response_text"]), ("error", "missing_or_invalid_usage", ANSWER))
        self.assertIsNone(row["input_tokens"])
        self.assertEqual(counts["attempts_with_unknown_cost"], 1)
        self.assertEqual(counts["reserved_cost_usd"], row["preflight_estimate_usd"])

    def test_reported_cost_above_quote_stops_before_next_request(self):
        body = response_body()
        body["usage"].update(input_tokens=30_000, total_tokens=30_025)
        _, counts, records = self.batch(lambda _: httpx2.Response(200, json=body))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(counts["stop"]["reason"], "estimate_exceeded")
        self.assertGreater(records[0]["estimated_cost"], records[0]["preflight_estimate_usd"])

    def test_cumulative_limit_stops_request_49_across_outputs_with_failure(self):
        path = self.output_root / "reservations.sqlite3"

        def reply(request):
            if len(self.requests) == 7:
                raise httpx2.ReadTimeout("simulated timeout", request=request)
            return httpx2.Response(200, json=response_body())

        _, first, records = self.batch(reply, ledger=path)
        self.assertEqual((first["ok"], first["failed"], first["g4_totals"]["requests_used"]), (23, 1, 24))
        self.assertEqual(records[6]["g4_request_number"], 7)
        _, second, records = self.batch(ledger=path, output_root=self.output_root / "another-output")
        self.assertEqual((second["ok"], second["g4_totals"]["requests_used"]), (24, 48))
        self.assertEqual((records[0]["g4_request_number"], records[-1]["g4_request_number"]), (25, 48))
        _, blocked, records = self.batch(ledger=path)
        self.assertEqual(len(self.requests), 48)
        self.assertEqual(blocked["stop"]["reason"], "request_limit_reached")
        self.assertEqual(blocked["g4_totals"]["requests_remaining"], 0)
        self.assertFalse(records)
        self.assertGreater(blocked["g4_totals"]["reserved_cost_usd"], 0)

    def test_cumulative_budget_survives_reopen_and_unsettled_dispatch(self):
        path = self.output_root / "reservations.sqlite3"
        with OpenAILedger(path, 48, 5) as ledger:
            first, reason = ledger.reserve("first", 1, "DEV_T01", "C0", Decimal("2.5"))
            self.assertIsNone(reason)
            ledger.settle(first, {"estimated_cost": 2, "status": "ok"}, None)
            pending, reason = ledger.reserve("first", 1, "DEV_T02", "C0", Decimal("2"))
            self.assertIsNone(reason)
        with OpenAILedger(path, 48, 5) as ledger:
            self.assertEqual(ledger.summary()["unsettled_requests"], 1)
            self.assertEqual(ledger.reserve("second", 1, "DEV_T01", "C0", Decimal("1.01")), (None, "budget_exhausted"))
            number, reason = ledger.reserve("second", 1, "DEV_T01", "C0", Decimal("1"))
            self.assertEqual((number, reason), (3, None))
            ledger.settle(pending, {"estimated_cost": None, "status": "error"}, None)
            self.assertEqual(ledger.summary()["reserved_cost_usd"], 3)

    def test_formal_and_unsafe_openai_settings_rejected(self):
        for changes in [{"mode": "formal"}, {"paid_calls_enabled": False}, {"budget_usd": 5.01},
                        {"request_limit": 49}, {"request_limit": True}, {"max_attempts": 2}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_config(dict(self.config, **changes))
        for changes in [{"store": True}, {"max_retries": 1}, {"model": "gpt-6-luna-test-version"},
                        {"reasoning_effort": "low"}, {"max_output_tokens": 1025}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_config(dict(self.config, openai=dict(self.config["openai"], **changes)))

    def test_sdk_construction_uses_environment_key_and_never_needs_a_dotenv_file(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": TEST_KEY}), patch("openai.OpenAI") as constructor, patch(
            "openai.DefaultHttpxClient",
        ) as http:
            model = OpenAIModel(self.config["openai"])
            model.close()
        http.assert_called_once_with(follow_redirects=False)
        constructor.assert_called_once_with(api_key=TEST_KEY, base_url="https://api.openai.com/v1", timeout=60,
                                            max_retries=0, http_client=http.return_value)
        constructor.return_value.close.assert_called_once()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), self.assertRaisesRegex(ValidationError, "OPENAI_API_KEY"):
            OpenAIModel(self.config["openai"])

    def test_cli_requires_explicit_opt_in_and_defaults_to_mock(self):
        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()), patch(
            "ai_gm_visibility.runner.OpenAIModel", side_effect=AssertionError("SDK must not be constructed"),
        ):
            self.assertEqual(main(["run", "--all", "--config", "configs/dev-openai.json"]), 2)
            self.assertEqual(main(["run", "--all", "--provider", "openai"]), 2)
            self.assertEqual(main(["run", "--all", "--output-root", str(self.output_root)]), 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["attempts"], 24)

    def test_evaluation_preserves_development_run_provenance(self):
        for synthetic in (True, False):
            with self.subTest(synthetic=synthetic):
                directory, _, _ = self.batch()
                path = directory / "run_manifest.json"
                manifest = load_json(path)
                # Simulate a saved run from the CLI's production construction path offline.
                manifest["synthetic"] = synthetic
                path.write_text(json.dumps(manifest), encoding="utf-8")
                records_path = directory / "records.jsonl"
                records_path.write_text("".join(
                    json.dumps(dict(row, synthetic=synthetic)) + "\n" for row in read_jsonl(records_path)
                ), encoding="utf-8")
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["evaluate", "--run", directory.name, "--output-root", str(self.output_root)]), 0)
                scores = load_json(json.loads(output.getvalue())["saved"])
                self.assertIs(scores["synthetic"], synthetic)
                self.assertFalse(scores["formal_result"])
                self.assertEqual(scores["semantic_status"], "NOT_RUN")
