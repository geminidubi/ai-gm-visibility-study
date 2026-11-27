"""Exercise the paid-study fence and the offline review paths with no network."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import OpenAI

from ai_gm_visibility.g5_review import export_run
from ai_gm_visibility.g5_rp import DATA
from ai_gm_visibility.g5_rp_review import FIELDS, make_packet, score, validate_judgment
from ai_gm_visibility.g5_study import approved, collect, dispatch, prepare
from ai_gm_visibility.logging_io import read_jsonl
from ai_gm_visibility.schemas import load_json
from test_openai_backend import TEST_KEY, response_body


class StudyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sent = []
        self.fail_at = None
        self.interrupt = False
        self.texts = []
        http = httpx2.Client(transport=httpx2.MockTransport(self.transport))
        self.client = OpenAI(api_key=TEST_KEY, max_retries=0, timeout=60, http_client=http)
        self.addCleanup(self.client.close)

    def transport(self, request):
        body = json.loads(request.content)
        self.sent.append(body)
        if self.interrupt:
            raise KeyboardInterrupt
        if self.fail_at == len(self.sent):
            return httpx2.Response(401, json={"error": {"message": "test authorization failure"}})
        reply = response_body(model=body["model"], max_output_tokens=body["max_output_tokens"], reasoning=body["reasoning"])
        if self.texts:
            reply["output"][0]["content"][0]["text"] = self.texts[len(self.sent) - 1]
        return httpx2.Response(200, json=reply)

    def test_study_approval_binds_scope_plan_and_budget(self):
        formal = prepare("formal", "none")
        low = prepare("formal", "low")
        cheap = prepare("rp", "none", "0.001")
        self.assertEqual(len(formal[4]), 1080)
        self.assertEqual({u["repeat_id"] for u in formal[2]}, {1, 2, 3})
        receipt = {"approved": True, "scope": "formal", "plan_id": formal[-1]["plan_id"], "approved_by": "test",
                   "pilot_assessment": "fixture", "formal_dataset_and_rubric_decisions": "fixture"}
        approved(formal, receipt)
        with self.assertRaisesRegex(ValueError, "match this study"):
            approved(low, receipt)
        with self.assertRaisesRegex(ValueError, "exceeds the approved cap"):
            approved(cheap, {**receipt, "scope": "rp", "plan_id": cheap[-1]["plan_id"]})
        with patch("ai_gm_visibility.g5_study.OpenAIModel", side_effect=AssertionError("SDK initialized")):
            with self.assertRaisesRegex(ValueError, "authorization required"):
                dispatch(formal, 1)

    def test_formal_stop_is_persistent_and_collects_a_partial_panel(self):
        plan = prepare("formal", "none")
        root = self.root / "simulation-formal"
        self.fail_at = 2
        result = dispatch(plan, 10, simulated_client=self.client, simulated_root=root)
        self.assertEqual((result["ok"], result["failed"], result["stop"]), (1, 1, "api_http_error"))
        self.assertEqual(result["ledger"]["attempts_with_unknown_cost"], 1)
        self.assertGreater(result["ledger"]["reserved_cost_usd"], 0)
        with self.assertRaisesRegex(ValueError, "Study is stopped"):
            dispatch(plan, 10, simulated_client=self.client, simulated_root=root)
        review = self.root / "formal-review"
        snapshot = collect(plan, review, simulated_root=root)
        self.assertEqual((snapshot["planned"], snapshot["successful"]), (1080, 1))
        bundle = self.root / "formal-blind"
        export_run(review, Path("data/g5/formal_candidate_gold.jsonl"), bundle)
        self.assertEqual(len(read_jsonl(bundle / "judge_packets.jsonl")), 1)
        self.assertEqual(len(self.sent), 2)

    def test_interrupted_reservation_blocks_further_dispatch(self):
        plan = prepare("rp", "none")
        root = self.root / "simulation-interrupt"
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            dispatch(plan, 1, simulated_client=self.client, simulated_root=root)
        self.interrupt = False
        with self.assertRaisesRegex(ValueError, "unresolved dispatches"):
            dispatch(plan, 1, simulated_client=self.client, simulated_root=root)
        self.assertEqual(len(self.sent), 1)

    def test_rp_resume_and_blind_review_use_the_calibration_shape(self):
        plan = prepare("rp", "low")
        defined = load_json(DATA / "cases.json")
        references = {r["case_id"]: r for r in load_json(DATA / "reference.json")["references"]}
        fixtures = load_json(DATA / "calibration.json")
        sample_by_text = {f["response"]: f for f in fixtures}
        sample_by_case = {f["case_id"]: f for f in fixtures}
        cases = {c["case_id"]: dict(c, definition=defined["worlds"][c["world"]]) for c in defined["cases"]}
        for fixture in fixtures:
            cid = fixture["case_id"]
            packet = make_packet("offline-fixture", cases[cid], references[cid], fixture["response"])
            identity = {k: packet[k] for k in ("blind_id", "packet_sha256")}
            validate_judgment({k: v for k, v in (fixture | identity).items() if k in FIELDS},
                              packet, fixture["response"], references[cid])
        self.texts = [sample_by_case[u["case_id"]]["response"] for u in plan[2]]
        root = self.root / "simulation-rp"
        first = dispatch(plan, 8, simulated_client=self.client, simulated_root=root)
        second = dispatch(plan, 18, simulated_client=self.client, simulated_root=root)
        done = dispatch(plan, 18, simulated_client=self.client, simulated_root=root)
        self.assertEqual((first["ok"], second["ok"], done["attempts"], len(self.sent)), (8, 10, 0, 18))
        review, bundle = self.root / "rp-review", self.root / "rp-blind"
        self.assertEqual(collect(plan, review, simulated_root=root)["successful"], 18)
        self.assertEqual(score("export", review, bundle)["work_batches"], 2)
        packets = read_jsonl(bundle / "judge_packets.jsonl")
        returned = []
        for packet in packets:
            self.assertFalse({"architecture", "case_id", "selected_fact_ids"} & packet.keys())
            identity = {k: packet[k] for k in ("blind_id", "packet_sha256")}
            returned.append({k: v for k, v in (sample_by_text[packet["response"]] | identity).items() if k in FIELDS})
        path = self.root / "work-return.jsonl"
        invalid = dict(returned[-1], packet_sha256="wrong")
        path.write_text("\n".join(map(json.dumps, [*returned[:-1], invalid])), encoding="utf-8")
        progress = score("import-work", review, bundle=bundle, inputs=[path], model="fixture", reviewer="fixture")
        self.assertEqual((progress["work_received_valid"], progress["import_status"]), (17, "COMMITTED_WITH_REJECTIONS"))
        resumed = score("resume-work", review, self.root / "resume", bundle)
        self.assertEqual(resumed["work_exports"]["packet_count"], 1)
        path.write_text(json.dumps(returned[-1]), encoding="utf-8")
        score("import-work", review, bundle=bundle, inputs=[path], model="fixture", reviewer="fixture")
        assessment = score("assess", review, self.root / "rp-assessment", bundle)
        self.assertEqual(assessment["human_sample"]["random_selected"], 3)
        self.assertEqual(assessment["by_architecture"]["C2"]["retrieval"]["retrieved"], 6)
