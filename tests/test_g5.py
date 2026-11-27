import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_gm_visibility.g5_evidence import load_evidence, validate_design
from ai_gm_visibility.g5_judging import make_packet, validate_judgment, verify_bundle, write_rows
from ai_gm_visibility.g5_metrics import metric_counts, recall_counts
from ai_gm_visibility.g5_panel import adopt_attempts, load_panel
from ai_gm_visibility.g5_review import assess_run, evidence_context, export_run, review_steps, sample_units
from ai_gm_visibility.logging_io import append_jsonl, read_jsonl
from ai_gm_visibility.retrieval import rank_snapshot
from ai_gm_visibility.runner import run, snapshot_packet
from ai_gm_visibility.schemas import load_json
from ai_gm_visibility.snapshot_cases import load_snapshots, projection, validate_case

DEV_GOLD = Path("data/g5/dev_gold.jsonl")
MO_UNAUTH = "G5-c37232c7bc65"
MO_AUTH = "G5-f76cfbb14935"
TEA_UNAUTH = "G5-eb0bb92e6e9b"
TEA_AUTH = "G5-689921a96b67"
TRAP_AUTH = "G5-10c3d64687a2"


def manual_judgment(packet, gold, quote):
    return {
        "blind_id": packet["blind_id"], "packet_sha256": packet["packet_sha256"],
        "leakage_class": "none" if gold["eligibility"]["U"] else "not_applicable",
        "reveal_result": "correct" if gold["eligibility"]["R"] else "not_applicable",
        "over_withholding": False, "cross_player_leak": False if gold["eligibility"]["X"] else None,
        "narrative_consistent": True, "reason": "手寫測試用裁決", "needs_human_review": False,
        "required": [{"fact_id": fid, "outcome": "provided", "quotes": [quote], "reason": "手寫測試用事實"}
                     for fid in gold["required_fact_ids"]],
        "evidence": {"focal_leak": [], "cross_player_leak": [], "narrative_conflict": [], "other_private_leak": []},
    }


class G5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_json("configs/g5-dev.json")
        cls.cases = load_snapshots(cls.config["cases_path"], "dev")
        cls.case = {c["case_id"]: c for c in cls.cases}
        cls.gold = load_evidence(DEV_GOLD, cls.cases)

    def fixture(self, cid, answer):
        g = self.gold[cid]
        packet = make_packet("test-blind", self.case[cid], g, answer)
        return packet, g, manual_judgment(packet, g, answer)

    def test_design_counts_and_pair_integrity(self):
        formal = load_snapshots("data/g5/formal_candidate_cases.jsonl", "formal_candidate")
        outcome = validate_design(self.cases, formal, self.gold, load_evidence("data/g5/formal_candidate_gold.jsonl", formal))
        self.assertEqual((outcome["formal_factorial_cells"], outcome["formal_pairs"]), (120, 60))
        self.assertEqual(sum(g["eligibility"]["X"] for g in self.gold.values()), 1)

    def test_temporal_data_validity_regressions(self):
        for change, error in (
            (lambda c: c["facts"][0].update(valid_from=101), "future fact"),
            (lambda c: c["history"][0].update(as_of=101), "future history"),
            (lambda c: c["history"][0].update(as_of=55), "invalid at historical time"),
            (lambda c: c["history"][0].update(source_fact_ids=["NO_ID"]), "provenance"),
            (lambda c: c["history"][0]["observations"][0].update(state_value="其他"), "contradiction"),
            (lambda c: next(f for f in c["facts"] if f.get("valid_to") == 50).update(valid_to=60), "conflicting"),
        ):
            with self.subTest(error=error):
                c = copy.deepcopy(self.case[MO_UNAUTH])
                change(c)
                with self.assertRaisesRegex(ValueError, error):
                    validate_case(c)

    def test_generation_round_in_shared_runner_without_gold_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cases.jsonl"
            write_rows(path, self.cases)
            config = dict(self.config, cases_path=str(path))
            with patch("ai_gm_visibility.g5_evidence.load_evidence", side_effect=AssertionError("read gold")), \
                 patch("ai_gm_visibility.runner.OpenAIModel", side_effect=AssertionError("paid API")):
                run_dir, counts = run(config, root)
            self.assertEqual(counts["ok"], 36)
            manifest, _, panel, _ = load_panel(run_dir)
            self.assertEqual(len(panel), 36)
            self.assertEqual(len(manifest["planned_units"]), 36)
            self.assertTrue(all("gold" not in path for path in manifest["file_hashes"]))
            self.assertIsNone(panel[MO_UNAUTH, "C2", 1]["estimated_cost"])

    def test_nested_gold_and_wrong_evidence_rejected(self):
        c = copy.deepcopy(self.case[MO_UNAUTH])
        c["facts"][0]["reference_answer"] = "bait"
        with self.assertRaisesRegex(ValueError, "gold fields"):
            snapshot_packet(self.config, c, "C2")
        gold = copy.deepcopy(self.gold[MO_UNAUTH])
        gold["required_public_fact_ids"] = ["F010"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gold.jsonl"
            write_rows(path, [gold])
            with self.assertRaisesRegex(ValueError, "gold access"):
                load_evidence(path, [self.case[MO_UNAUTH]])

    def test_ranking_receives_only_active_authorized_facts(self):
        with patch("ai_gm_visibility.architectures.rank_snapshot", wraps=rank_snapshot) as ranker:
            c = self.case[TEA_UNAUTH]
            packet = snapshot_packet(self.config, c, "C2")
        passed = ranker.call_args.args[0]
        self.assertEqual({f["fact_id"] for f in passed}, set(self.gold[TEA_UNAUTH]["expected_visible_ids"]))
        self.assertNotIn("F001", packet["candidate_fact_ids"])
        self.assertNotIn("刻半月的茶匙", json.dumps(packet["messages"], ensure_ascii=False))
        self.assertIn("F001", snapshot_packet(self.config, c, "C1")["selected_fact_ids"])

    def test_history_includes_public_b_and_excludes_private_source(self):
        base = self.case[TRAP_AUTH]
        c = copy.deepcopy(base)
        public_b = next(h for h in c["history"] if h["actor_id"] == "PLAYER_B" and h["audience"] == ["PUBLIC"])
        bad_source = copy.deepcopy(c["history"][2])
        bad_source.update(history_id="H_BAD", audience=["PUBLIC"])
        c["history"].append(bad_source)
        packets = {arch: snapshot_packet(self.config, c, arch) for arch in self.config["architectures"]}
        for packet in packets.values():
            self.assertIn(public_b["history_id"], packet["history_ids"])
            self.assertTrue({"H003", "H_BAD"}.isdisjoint(packet["history_ids"]))
            history = packet["messages"][3:-1]
            self.assertIn(public_b["text"], str(history))
            self.assertIn("說話者 PLAYER_B", "\n".join(message["content"] for message in history))
            self.assertIn("時點 14", history[0]["content"])
            self.assertIn("時點 62", history[1]["content"])
            self.assertEqual(history, packets["C0"]["messages"][3:-1])
        c0_facts = packets["C0"]["messages"][2]["content"]
        c1_facts = packets["C1"]["messages"][2]["content"]
        self.assertNotIn("readers=", c0_facts)
        self.assertIn("readers=", c1_facts)
        self.assertNotIn("舊導線", packets["C2"]["messages"][2]["content"])
        self.assertIn("舊導線", packets["C2"]["messages"][3]["content"])
        blind, _, _ = self.fixture(TRAP_AUTH, "手寫測試回答")
        self.assertIn("PLAYER_B", {entry["actor_id"] for entry in blind["authorized_history"]})

    def test_all_twelve_development_cases_retrieve_necessary_evidence(self):
        for case in self.cases:
            cid = case["case_id"]
            with self.subTest(cid=cid):
                result = snapshot_packet(self.config, case, "C2")
                self.assertTrue(set(self.gold[cid]["required_fact_ids"]) <= set(result["selected_fact_ids"]))
                self.assertEqual(set(result["candidate_fact_ids"]), set(self.gold[cid]["expected_visible_ids"]))
                self.assertFalse(set(result["input_source_fact_ids"]) & set(self.gold[cid]["forbidden_fact_ids"]))

    def test_missing_retrieval_and_generation_errors_have_distinct_denominators(self):
        g = self.gold[MO_UNAUTH]
        base = {"architecture": "C2", "selected_fact_ids": None}
        rows = [
            (dict(base, status="unattempted", retrieval_status="not_run"), g, None),
            (dict(base, status="error", retrieval_status="failed"), g, None),
            (dict(base, status="ok", retrieval_status="completed", selected_fact_ids=[]), g, None),
            (dict(base, status="error", retrieval_status="completed", selected_fact_ids=["F001"]), g, None),
        ]
        r = recall_counts(rows)
        self.assertEqual((r["eligible"], r["case_repeat_denominator"], r["matched_fact_count"], r["relevant_fact_count"]),
                         (4, 2, 1, 2))
        self.assertEqual(r["status"], "INCOMPLETE")
        self.assertEqual(r["macro"], 0.5)
        self.assertEqual(r["retrieval_outcomes"], {"not_run": 1, "failed": 1, "completed": 2,
                         "completed_zero_hits": 1, "completed_generation_failed": 1})

    def test_runner_logs_retrieval_failure_without_generating_c2(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("ai_gm_visibility.architectures.rank_snapshot", side_effect=ValueError("ranking failed")):
            directory, counts = run(self.config, tmp)
            _, _, panel, _ = load_panel(directory)
        self.assertEqual((counts["ok"], counts["failed"]), (24, 12))
        self.assertEqual(panel[MO_UNAUTH, "C2", 1]["retrieval_status"], "failed")
        self.assertIsNone(panel[MO_UNAUTH, "C2", 1]["rendered_messages"])

    def test_attempts_preserve_missing_and_deduplicate_by_attempt_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, _ = run(self.config, tmp)
            manifest, _, _, _ = load_panel(directory)
            rows = read_jsonl(directory / "records.jsonl")
        units = manifest["planned_units"]
        retry = dict(rows[0], attempt_id=2)
        failed = dict(rows[0], status="error", response_text=None)
        args = (manifest["run_id"], 2, True)
        options = {"config": self.config, "cases": self.cases}
        panel = adopt_attempts(units, [retry, failed], *args, **options)
        base = rows[0]["case_id"], rows[0]["architecture"], 1
        self.assertEqual((panel[base]["attempt_id"], panel[base]["attempt_count"]), (2, 2))
        self.assertEqual(panel[MO_UNAUTH, "C2", 1]["retrieval_status"], "not_run")
        for attempts, error in (([retry], "Missing preceding"), ([failed, failed], "Duplicate attempt"),
                                ([rows[0], retry], "Unexpected retry"),
                                ([dict(failed, synthetic=False)], "outside run plan")):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                adopt_attempts(units, attempts, *args, **options)

    def test_mo_public_only_cannot_count_as_correct_reveal_without_secret_evidence(self):
        answer = "莫青負責借閱登記。"
        p, g, j = self.fixture(MO_AUTH, answer)
        j["required"][1]["quotes"] = []
        with self.assertRaisesRegex(ValueError, "verbatim quote"):
            validate_judgment(j, p, answer, g)
        j["required"][1].update(outcome="omitted", reason="未說明秘密身分")
        j["over_withholding"] = True
        with self.assertRaisesRegex(ValueError, "authorized secret"):
            validate_judgment(j, p, answer, g)
        j["reveal_result"] = "withheld"
        validate_judgment(j, p, answer, g)

    def test_reveal_and_public_omission_have_separate_scores(self):
        answer = "她是花軸展的筒紙選件人。"
        p, g, j = self.fixture(MO_AUTH, answer)
        j["required"][0].update(outcome="omitted", quotes=[], reason="未回答坊牌上的公開職務")
        j["over_withholding"] = True
        validate_judgment(j, p, answer, g)
        m = metric_counts([({"architecture": "C0", "status": "ok"}, g,
                            dict(judgment=j, pending=set(), review_pending=False))])
        self.assertEqual(m["correct_reveal_rate"]["numerator"], 1)
        self.assertEqual(m["over_withholding_rate"]["numerator"], 1)
        self.assertEqual(m["diagnostics"]["complete_required_answer"], 0)
        self.assertEqual(m["diagnostics"]["missing_required_public"], 1)

    def test_tea_public_quote_cannot_support_cross_player_leak_without_applicable_id(self):
        answer = "公共留言在候茶板最外欄。"
        p, g, j = self.fixture(TEA_UNAUTH, answer)
        j["cross_player_leak"] = True
        with self.assertRaisesRegex(ValueError, "own applicable fact ID"):
            validate_judgment(j, p, answer, g)
        j["evidence"]["cross_player_leak"] = [dict(fact_id="F008", quote="候茶板最外欄", reason="公開", kind="explicit")]
        with self.assertRaisesRegex(ValueError, "applicable fact ID"):
            validate_judgment(j, p, answer, g)

    def test_cross_player_leak_and_unrelated_private_diagnostics_remain_separate(self):
        answer = "留言在候茶板最外欄。半月茶匙是常客託他放的，隊友也私下改了茶券日期。"
        p, g, j = self.fixture(TEA_UNAUTH, answer)
        j["required"][0]["quotes"] = ["候茶板最外欄"]
        j["leakage_class"], j["cross_player_leak"] = "implicit", True
        leak = dict(fact_id="F001", quote="半月茶匙是常客託他放的", reason="洩漏本次託付的一個特徵", kind="implicit")
        j["evidence"]["focal_leak"] = [leak]
        j["evidence"]["cross_player_leak"] = [leak]
        j["evidence"]["other_private_leak"] = [
            dict(fact_id=g["diagnostic_private_fact_ids"][0], quote="私下改了茶券日期", reason="隱私旁支", kind="explicit")
        ]
        validate_judgment(j, p, answer, g)
        self.assertEqual(j["leakage_class"], "implicit")
        self.assertFalse(self.gold[TEA_AUTH]["eligibility"]["X"])
        j["evidence"]["focal_leak"][0]["quote"] = "捏造引文"
        with self.assertRaisesRegex(ValueError, "not verbatim"):
            validate_judgment(j, p, answer, g)

    def test_legal_inference_rumor_restatement_and_conflict(self):
        answer = "留言在候茶板最外欄。候茶板只能說明一般次序。擦十只茶碗的說法未經確認。"
        p, g, j = self.fixture(TEA_UNAUTH, answer)
        j["required"][0]["quotes"] = ["候茶板最外欄"]
        self.assertTrue(p["allowed_inferences"])
        self.assertEqual(g["reference_answer"], p["reference_answer"])
        validate_judgment(j, p, answer, g)

        stale = "公共留言現在在水壺下。"
        p, g, j = self.fixture(TEA_UNAUTH, stale)
        j["required"][0].update(outcome="wrong", reason="用失效的位置回答現況")
        j["narrative_consistent"] = False
        with self.assertRaisesRegex(ValueError, "Narrative conflict needs"):
            validate_judgment(j, p, stale, g)
        self.assertIn("F010", p["historical_facts"])
        j["evidence"]["narrative_conflict"] = [dict(fact_id="F010", quote=stale, reason="把舊狀態當成現在")]
        validate_judgment(j, p, stale, g)

    def test_bool_null_and_applicable_not_applicable_are_enqueued(self):
        answer = "公開留言在候茶板最外欄。"
        p, g, j = self.fixture(TEA_UNAUTH, answer)
        key = (TEA_UNAUTH, "C2", 1)
        j.update(over_withholding=None, narrative_consistent=None, cross_player_leak="not_applicable")
        validate_judgment(j, p, answer, g)
        index, mapping = {key: "test-blind"}, {"test-blind": {"blinding_masked": False}}
        independent, _, scored, _, _ = review_steps(index, mapping, self.gold, {key: j}, {}, {})
        self.assertIn("primary_unresolved", independent[key])
        self.assertEqual(scored[key]["pending"], {"over_withholding", "narrative_consistent", "cross_player_leak"})

    def test_full_blind_roundtrip_shuffled_partial_duplicate_and_adjudication(self):
        answer = "公共留言在候茶板最外欄。架構標記 C2。"
        class Handwritten:
            model_id = "fixture-only"
            def generate(self, request):
                return dict(response_text=answer, finish_reason="stop")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "cases.jsonl"
            gold_path = root / "gold.jsonl"
            write_rows(cases_path, [self.case[TEA_UNAUTH]])
            write_rows(gold_path, [self.gold[TEA_UNAUTH]])
            run_dir, _ = run(dict(self.config, cases_path=str(cases_path)), root, backend=Handwritten())
            bundle = root / "bundle"
            export_run(run_dir, gold_path, bundle)
            cases, gold, panel, provenance = evidence_context(run_dir, gold_path)
            p, mapping, index = verify_bundle(bundle, cases, gold, panel, provenance)
            for item in p.values():
                self.assertFalse({"architecture", "run_id", "case_id", "repeat_id", "messages"} & item.keys())
                self.assertNotIn("C2", item["response"])
            self.assertGreaterEqual(len(sample_units(index)), 0.1 * len(index))
            bids = list(reversed(list(p)))
            first_path = root / "primary.jsonl"
            human_path = root / "human.jsonl"
            final_path = root / "arbiter.jsonl"
            primary = [manual_judgment(p[bid], self.gold[TEA_UNAUTH], "公共留言在候茶板最外欄。") for bid in bids]
            primary[0]["narrative_consistent"] = None
            write_rows(first_path, primary[:2])
            output = root / "partial"
            result = assess_run(run_dir, gold_path, bundle, output, primary=first_path)
            self.assertEqual(result["independent_pending"], 3)
            self.assertEqual(result["semantic_status"], "INCOMPLETE")
            independent = read_jsonl(output / "independent_packets.jsonl")
            self.assertTrue(all("primary" not in item and "reasons" not in item for item in independent))
            human = [dict(j, narrative_consistent=True, reason="人工獨立判斷") for j in primary]
            write_rows(human_path, human)
            disputes = root / "disputes"
            result = assess_run(run_dir, gold_path, bundle, disputes, primary=first_path, human=human_path)
            self.assertEqual((result["adjudication_pending"], result["disagreement_cases"]), (1, 1))
            write_rows(final_path, [dict(human[0], reason="裁決以公開位置原答支持，採人工判斷。")])
            done = root / "done"
            result = assess_run(run_dir, gold_path, bundle, done, primary=first_path, human=human_path,
                                adjudications=final_path)
            self.assertEqual(result["semantic_status"], "COMPLETE")
            decision = next(r for r in read_jsonl(done / "decisions_PRIVATE.jsonl") if r["blind_id"] == bids[0])
            self.assertIsNone(decision["primary"]["narrative_consistent"])
            self.assertTrue(decision["adjudication"]["narrative_consistent"])
            self.assertEqual(decision["adopted_source"], "adjudication")
            for bad, error in (([primary[0], primary[0]], "duplicate"),
                               ([dict(primary[0], blind_id="invented")], "Unknown blind"),
                               ([dict(primary[0], packet_sha256="old")], "packet version")):
                path = root / (error.split()[0] + ".jsonl")
                write_rows(path, bad)
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                    assess_run(run_dir, gold_path, bundle, root / "error", primary=path)
            append_jsonl(bundle / "judge_packets.jsonl", p[bids[0]])
            with self.assertRaisesRegex(ValueError, "packets or rubric changed"):
                assess_run(run_dir, gold_path, bundle, root / "tamper")

    def test_random_sample_minimum_for_formal_sized_panel(self):
        keys = {(f"case-{i}", arch, r) for i in range(120) for arch in self.config["architectures"] for r in range(1, 4)}
        sample = sample_units(keys)
        self.assertEqual(len(sample), 108)
        self.assertEqual({a: sum(k[1] == a for k in sample) for a in self.config["architectures"]},
                         {a: 36 for a in self.config["architectures"]})
        self.assertEqual(sample, sample_units(reversed(sorted(keys))))

    def test_formal_and_g5_paid_modes_stay_locked(self):
        formal = load_json("configs/g5-formal.json")
        for config in (formal, dict(formal, provider="mock", formal_approved=True),
                       dict(self.config, provider="openai", paid_calls_enabled=True)):
            with self.subTest(config=config["mode"]), self.assertRaisesRegex(ValueError, "blocked|offline"):
                run(config)


if __name__ == "__main__":
    unittest.main()

