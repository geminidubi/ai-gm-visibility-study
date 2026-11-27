"""Rebuild the G5 offline reports from actual assembled messages. No paid calls."""

import argparse
import csv
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from ai_gm_visibility.g5_evidence import load_evidence, validate_design
from ai_gm_visibility.g5_judging import RUBRIC, make_packet
from ai_gm_visibility.g5_metrics import recall_counts
from ai_gm_visibility.g5_panel import load_panel
from ai_gm_visibility.g5_work import DEFAULT_BATCH_SIZE, INSTRUCTIONS
from ai_gm_visibility.llm.openai_backend import INPUT_PADDING_TOKENS, PRICING, SHORT_CONTEXT_LIMIT
from ai_gm_visibility.logging_io import file_hash, read_jsonl, write_json
from ai_gm_visibility.runner import run, snapshot_packet
from ai_gm_visibility.schemas import load_json, require
from ai_gm_visibility.snapshot_cases import DATASET_VERSION, load_snapshots, projection

ROOT = Path("data/g5")
SOURCE_ZIP_SHA256 = "17228db5be2edcb0b1e71397ba5f0989fdbdf2d98d6052f9781707fdff8d6223"
STRATEGIES = {
    "v01_ranker_on_v02_at_8": ("overlap_v01", 8),
    "bm25_at_4": ("clause_bm25_v1", 4),
    "bm25_at_8": ("clause_bm25_v1", 8),
    "selected_bm25_at_10": ("clause_bm25_v1", 10),
    "all_authorized": ("all_authorized", None),
}
# Work's independently verified baseline, on v0.1 data and v0.1 history projection.
WORK_BASELINE = {"cases_complete": 9, "cases": 12, "matched_facts": 15, "required_facts": 18, "macro": 0.875,
                 "target_ranks": {"G5-10c3d64687a2": 31, "G5-cb6dbf5ecc9f": 15, "G5-5e36c76e9a80": 39}}


def utf8_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def check_message(case, record, g):
    facts = {f["fact_id"]: f["content"] for f in case["facts"]}
    active, visible, history = projection(case)
    messages = record["rendered_messages"]
    arch = record["architecture"]
    selected = record["selected_fact_ids"]
    full = set(selected)
    require(g["expected_visible_ids"] == sorted(visible) == record["visible_fact_ids"], "Visibility mismatch")
    require(record["history_ids"] == [h.history_id for h in history], "Wrong fixed history")
    require(messages[3:-1] == [
        {"role": "system", "content": f"核定角色歷史（時點 {h.as_of}）：" + h.text} for h in history
    ], "Actual historical message mismatch")
    require(messages[-1] == {"role": "user", "content": case["query"]}, "Question changed")
    background, evidence = messages[2]["content"].split("證據：\n", 1)
    actual = []
    for line in evidence.splitlines():
        match = re.fullmatch(r"\[(F\d+)(?: readers=([^\]]+))?\] (.+)", line)
        require(match, "Rendered evidence has unknown format")
        fid, readers, content = match.groups()
        require(content == facts[fid], "Rendered evidence differs from source")
        if arch == "C1":
            source = next(f for f in case["facts"] if f["fact_id"] == fid)
            require(readers is not None and set(readers.split(",")) == set(source["readers"]), "Bad C1 reader labels")
        else:
            require(readers is None, "Unexpected access label")
        actual.append(fid)
    require(actual == selected, "Selected source log differs from message")
    require(set(record["input_source_fact_ids"]) == full | {fid for h in history for fid in h.source_fact_ids},
            "Input source provenance does not match message")
    require(set(record["candidate_fact_ids"]) == (visible if arch == "C2" else active), "Wrong retrieval candidate pool")
    if arch == "C2":
        require(full <= visible and not set(record["input_source_fact_ids"]) & set(g["forbidden_fact_ids"]),
                "Unauthorized C2 input")
        require(all(facts[fid] not in background for fid in g["forbidden_fact_ids"]), "Unauthorized C2 background")
    else:
        require(full == active and all(facts[fid] in background for fid in visible), "Full context or known background differs")
    return {
        "candidate_count": len(record["candidate_fact_ids"]), "selected_fact_ids": selected,
        "input_forbidden_fact_ids": sorted(set(record["input_source_fact_ids"]) & set(g["forbidden_fact_ids"])),
        "history_ids": record["history_ids"], "message_utf8_bytes": utf8_bytes(messages),
    }


def development_report(cfg, directory, cases, gold):
    manifest, _, panel, provenance = load_panel(directory)
    rows = []
    for case in cases:
        cid = case["case_id"]
        g = gold[cid]
        targets = g["relevant_visible_ids"]
        facts = {f["fact_id"]: f["content"] for f in case["facts"]}
        arms = {arch: check_message(case, panel[cid, arch, 1], g) for arch in cfg["architectures"]}
        comparisons = {}
        for name, (method, k) in STRATEGIES.items():
            packet = snapshot_packet(cfg, case, "C2", retrieval=method, top_k=k or len(facts))
            ranking = packet["ranked_facts"]
            ranks = {entry["fact_id"]: i for i, entry in enumerate(ranking, 1)}
            misses = [fid for fid in targets if fid not in packet["selected_fact_ids"]]
            comparisons[name] = {
                "selected_fact_ids": packet["selected_fact_ids"], "ranks_of_required_facts":
                {fid: ranks[fid] for fid in targets} if method != "all_authorized" else None,
                "selected_count": len(packet["selected_fact_ids"]), "candidate_count": len(ranking),
                "hit_count": len(targets) - len(misses), "required_count": len(targets),
                "missing_required_ids": misses, "case_recall": 1 - len(misses) / len(targets),
                "message_utf8_bytes": utf8_bytes(packet["messages"]),
                "ranked_facts": ranking if method != "all_authorized" else None,
            }
        require(arms["C2"]["selected_fact_ids"] == comparisons["selected_bm25_at_10"]["selected_fact_ids"],
                "Diagnostic ranking differs from actual run")
        included = set(arms["C2"]["history_ids"])
        rows.append({
            "case_id": cid, "family_id": case["family_id"], "pair_id": case["pair_id"], "question": case["query"],
            "difficulty": case["difficulty"], "query_type": case["query_type"], "authorization": case["authorization"],
            "reference_answer": g["reference_answer"],
            "required_evidence": {fid: facts[fid] for fid in targets}, "required_public_ids": g["required_public_fact_ids"],
            "required_secret_ids": g["required_reveal_fact_ids"], "forbidden_fact_ids": g["forbidden_fact_ids"],
            "focal_forbidden_ids": g["focal_forbidden_fact_ids"], "as_of": case["as_of"],
            "b_public_history_included": [h["history_id"] for h in case["history"] if h["actor_id"] == "PLAYER_B"
                                          and "PUBLIC" in h["audience"] and h["history_id"] in included],
            "history_excluded": [h["history_id"] for h in case["history"] if h["history_id"] not in included],
            "arms": arms, "retrieval": comparisons,
        })
    summary = {}
    for name in STRATEGIES:
        entries = [row["retrieval"][name] for row in rows]
        summary[name] = {
            "complete_cases": sum(not e["missing_required_ids"] for e in entries), "case_count": len(entries),
            "matched_facts": sum(e["hit_count"] for e in entries), "required_facts": sum(e["required_count"] for e in entries),
            "macro": sum(e["case_recall"] for e in entries) / len(entries),
            "message_bytes_total": sum(e["message_utf8_bytes"] for e in entries),
        }
    recall = recall_counts([(row, gold[row["case_id"]], None) for row in panel.values()])
    require(recall["macro"] == 1 and recall["status"] == "COMPLETE", "Development evidence acceptance failed")
    return dict(
        dataset_version=DATASET_VERSION, synthetic=True, semantic_status="NOT_RUN",
        run_evidence={"run_id": manifest["run_id"], "run_manifest_sha256": provenance["run_manifest"],
                      "raw_attempts_sha256": provenance["raw_attempts"]},
        work_v01_baseline=WORK_BASELINE, selected_retrieval_recall=recall,
        strategy_comparison=summary, cases=rows,
        unauthorized_inputs_by_arm={
            arch: sum(bool(r["arms"][arch]["input_forbidden_fact_ids"]) for r in rows) for arch in cfg["architectures"]
        },
    )


def estimate(config, cases, gold):
    repeats = config["repeats"]
    generation_out = config["planning_only_openai"]["max_output_tokens"]
    rubric_text = RUBRIC.read_text(encoding="utf-8")
    size_rows, judge_bytes = [], []
    # Generation assembly occurs against case facts only. Gold is used solely in the independent judge-size estimate.
    for case in cases:
        for arch in config["architectures"]:
            request = snapshot_packet(config, case, arch)
            size_rows.append({
                "case_id": case["case_id"], "architecture": arch, "difficulty": case["difficulty"],
                "message_bytes": utf8_bytes(request["messages"]),
            })
    for case in cases:
        packet = make_packet("0" * 32, case, gold[case["case_id"]], "")
        judge_bytes.append(utf8_bytes([
            {"role": "system", "content": rubric_text},
            {"role": "user", "content": json.dumps(packet, ensure_ascii=False, separators=(",", ":"))},
        ]))
    n = len(size_rows) * repeats
    generation_input = repeats * sum(row["message_bytes"] + INPUT_PADDING_TOKENS for row in size_rows)
    work_input = repeats * len(config["architectures"]) * sum(
        size + INPUT_PADDING_TOKENS + generation_out * 2 for size in judge_bytes
    )
    max_gen = max(row["message_bytes"] for row in size_rows) + INPUT_PADDING_TOKENS
    max_work = max(judge_bytes) + INPUT_PADDING_TOKENS + generation_out * 2
    require(max_gen <= SHORT_CONTEXT_LIMIT, "Short-context proxy exceeded")
    generation_cost = (generation_input * Decimal(PRICING["input"]) + n * generation_out * Decimal(PRICING["output"])) / 1_000_000

    def money(value):
        return float(round(value, 6))

    return {
        "status": "PLANNING_ONLY", "paid_calls": 0, "generation_pricing": PRICING,
        "semantic_judge": {"route": "ChatGPT Work batch files", "separate_paid_judge_api_requests": 0},
        "proxy": {
            "text": "Serialized UTF-8 bytes as token proxy, plus envelope allowance per request. No tokenizer or billed usage.",
            "envelope_units_per_request": INPUT_PADDING_TOKENS,
            "generation_output_tokens_per_request": generation_out,
            "additional_work_input_units_per_answer_for_response": generation_out * 2,
            "maximum_generation_input_proxy_per_request": max_gen, "maximum_work_input_proxy_per_answer": max_work,
        },
        "single_turn_candidates": {
            "generation_requests": n, "generation_input_proxy": generation_input, "generation_usd": money(generation_cost),
            "paid_api_requests_total": n, "work_questions_to_judge": n, "work_input_proxy": work_input,
            "work_batches_at_target_if_byte_limit_does_not_split": (n + DEFAULT_BATCH_SIZE - 1) // DEFAULT_BATCH_SIZE,
            "generation_with_illustrative_20_percent_reserve_usd": money(generation_cost * Decimal("1.2")),
            "random_human_reviews_at_full_coverage": n // 10,
        },
        "full_design_if_multi_turn_matches_single_turn_mean_cost_per_request": [
            {
                "scenarios": 8, "turns_per_scenario": turns, "additional_generation_requests": 8 * turns * 9,
                "generation_requests": n + 8 * turns * 9, "work_questions_to_judge": n + 8 * turns * 9,
                "generation_usd": money(generation_cost * (n + 8 * turns * 9) / n),
            }
            for turns in (6, 10)
        ],
        "formal_generation_message_sizes": size_rows,
    }


def review_index(path, cases, gold):
    fields = ["case_id", "family_id", "pair_id", "split", "difficulty", "secret_type", "query_type", "authorization",
              "query", "required_public_fact_ids", "required_reveal_fact_ids", "focal_forbidden_fact_ids",
              "reference_answer", "scoring_notes"]
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for case in cases:
            g = gold[case["case_id"]]
            writer.writerow(case | {field: ",".join(g[field]) if isinstance(g[field], list) else g[field]
                                    for field in fields if field in g})


def check_g4():
    expected = load_json("results/g4/raw-sha256.json")
    checked, present, rows = 0, 0, []
    for run_id, files in expected.items():
        rows.extend(read_jsonl(Path("results/g4") / run_id / "records.jsonl"))
        for name, sha in files.items():
            path = Path("runs") / run_id / name
            checked += 1
            if path.exists():
                require(file_hash(path) == sha, f"G4 raw file changed: {run_id}/{name}")
                present += 1
    require(len(rows) == 48 and all(row["status"] == "ok" and row["synthetic"] is False for row in rows),
            "G4 archive must contain 48 real successful results")
    return {"archived_real_successes": len(rows), "raw_hashes_expected": checked, "raw_hashes_verified_locally": present}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, default=Path("runs"))
    args = parser.parse_args()
    dev_config, formal_config = (load_json(f"configs/g5-{split}.json") for split in ("dev", "formal"))
    dev = load_snapshots(dev_config["cases_path"], "dev")
    formal = load_snapshots(formal_config["cases_path"], "formal_candidate")
    dev_gold = load_evidence(ROOT / "dev_gold.jsonl", dev)
    formal_gold = load_evidence(ROOT / "formal_candidate_gold.jsonl", formal)
    validation = validate_design(dev, formal, dev_gold, formal_gold)
    directory, counts = run(dev_config, args.run_root)
    diagnosis = development_report(dev_config, directory, dev, dev_gold)
    cost = estimate(formal_config, formal, formal_gold)
    validation.update(
        g4=check_g4(), dev_mock_counts=counts, paid_calls=0, semantic_status="NOT_RUN",
        eligible_units_per_architecture_per_repeat={
            split: dict(Counter(kind for g in gold.values() for kind, flag in g["eligibility"].items() if flag))
            for split, gold in (("dev", dev_gold), ("formal_candidate", formal_gold))
        },
    )
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in (("validation", validation), ("dev-retrieval", diagnosis), ("cost-estimate", cost)):
        write_json(args.output / f"{name}.json", value)
    review_index(args.output / "review-index.tsv", dev + formal, dev_gold | formal_gold)
    paths = (sorted(ROOT.glob("*")) + sorted(Path("prompts").glob("*.txt"))
             + [Path("configs/g5-dev.json"), Path("configs/g5-formal.json"), Path("pyproject.toml"), Path("uv.lock"),
                Path("docs/g5-workflow.md"), Path("docs/g5-v0.2-work-review.md"), RUBRIC, INSTRUCTIONS]
             + sorted(Path("scripts").glob("*g5*.py")) + sorted(Path("tests").glob("test_g5*.py"))
             + sorted(Path("src/ai_gm_visibility").rglob("*.py")))
    reports = sorted(args.output.iterdir())
    write_json(args.output / "release-manifest.json", {
        "status": "CANDIDATE_NOT_FROZEN", "dataset_version": DATASET_VERSION, "source_v01_zip_sha256": SOURCE_ZIP_SHA256,
        "history_policy": dev_config["history_policy"], "retrieval": {"ranking": dev_config["retrieval"], "top_k": dev_config["top_k"]},
        "file_sha256": {path.as_posix(): file_hash(path) for path in paths if path.is_file()},
        "report_sha256": {path.name: file_hash(path) for path in reports},
    })
    print(json.dumps({"output": str(args.output), "local_raw_dev_run": str(directory), "validation": validation},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

