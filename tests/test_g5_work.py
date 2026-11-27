import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ai_gm_visibility.g5_work import export_batches, read_submission
from ai_gm_visibility.logging_io import file_hash, read_jsonl
from scripts.demo_g5_work import run_demo


class WorkJudgingTests(unittest.TestCase):
    def test_full_36_response_work_roundtrip_and_safe_shared_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "demo"
            shared = root / "share.zip"
            report = run_demo(output, shared)
            self.assertEqual((report["development_cases"], report["responses"]), (12, 36))
            self.assertEqual(report["valid_work_rows_after_resume"], 36)
            self.assertEqual(report["assessment_status_after_arbitration"], "COMPLETE")
            self.assertEqual(report["paid_api_requests"], 0)
            self.assertEqual(report["random_sample"]["random_selected"], 6)
            self.assertGreaterEqual(report["independent_reviews"], report["random_sample"]["random_selected"])
            self.assertEqual(report["adjudication_cases"], 2)
            with zipfile.ZipFile(shared) as archive:
                expected = {"judge_rubric.md", "work_instructions.md", "manifest.json"}
                manifest = json.loads(archive.read("manifest.json"))
                expected.update(batch["file"] for batch in manifest["batches"])
                self.assertEqual(set(archive.namelist()), expected)
                packets = [json.loads(line) for batch in manifest["batches"]
                           for line in archive.read(batch["file"]).splitlines()]
                self.assertEqual(len({p["blind_id"] for p in packets}), 36)
                self.assertTrue(all({"packet_sha256", "reference_answer", "required_fact_ids", "allowed_inferences"}
                                    <= p.keys() and "architecture" not in p for p in packets))

    def test_batch_size_and_utf8_byte_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packets = [{"blind_id": str(i), "text": "中文字" * 10} for i in range(5)]
            manifest = export_batches(root / "counts", packets, batch_size=2)
            self.assertEqual([b["count"] for b in manifest["batches"]], [2, 2, 1])
            for batch in manifest["batches"]:
                path = root / "counts" / batch["file"]
                self.assertEqual(file_hash(path), batch["sha256"])
                self.assertEqual(path.stat().st_size, batch["utf8_bytes"])
            manifest = export_batches(root / "bytes", packets, batch_size=10, max_bytes=1)
            self.assertEqual(len(manifest["batches"]), 5)
            self.assertTrue(all(b["over_byte_target"] for b in manifest["batches"]))
            self.assertEqual(read_jsonl(root / "bytes" / manifest["batches"][-1]["file"]), [packets[-1]])

    def test_missing_and_duplicate_json_fields_are_diagnosed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "work.jsonl"
            path.write_text('{"blind_id":"a","blind_id":"b"}\n{}\n{"blind_id":"c"}\n', encoding="utf-8-sig")
            _, rows = read_submission(path)
            self.assertIn("Duplicate JSON member", rows[0][3])
            self.assertIn("Missing blind_id", rows[1][3])
            self.assertEqual(rows[2][1], "c")


if __name__ == "__main__":
    unittest.main()

