"""Rebuild inputs and verify the published HW5 results from archived judgments."""

from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ai_gm_visibility import g5_judging, g5_rp, g5_rp_review
from ai_gm_visibility.g5_evidence import load_evidence, validate_design
from ai_gm_visibility.g5_metrics import aggregate, decision_signature
from ai_gm_visibility.g5_panel import unit_key
from ai_gm_visibility.g5_records import RECORD_VERSION, reconcile_attempt
from ai_gm_visibility.g5_review import needs_independent
from ai_gm_visibility.g5_study import prepare
from ai_gm_visibility.g5_study_sampling import choose_sample, formal_slots, rp_candidates
from ai_gm_visibility.logging_io import digest, file_hash, read_jsonl
from ai_gm_visibility.runner import snapshot_packet
from ai_gm_visibility.schemas import load_json, require
from ai_gm_visibility.snapshot_cases import load_snapshots

FINAL = Path("results/g5/approved-2026-11-26")
PILOT = Path("results/g5/pilot")
HW5 = Path("results/hw5")
G4 = Path("results/g4")
ARCHITECTURES = ("C0", "C1", "C2")
REVIEWS = ("primary", "independent_human", "adjudication", "adopted_judgment")
TOKENS = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "cache_write_tokens")


def index(rows):
    result = {unit_key(row): row for row in rows}
    require(len(result) == len(rows), "Repeated case, architecture and repeat key")
    return result


def validate_response(row):
    text = row["response_text"]
    expected = sha256(text.encode()).hexdigest() if text is not None else None
    require(row["source"]["response_utf8_sha256"] == expected, f"Response checksum mismatch: {unit_key(row)}")
    require(digest(row["generation_request"]) == row["generation_request_sha256"],
            f"Request checksum mismatch: {unit_key(row)}")
    require(row["generation_request"]["input"] == row["rendered_messages"],
            f"Generation from a different input: {unit_key(row)}")


def decision_rows(path, panel, cases, gold, make_packet):
    rows = index(read_jsonl(path))
    require(rows.keys() == panel.keys(), f"Incomplete judgments: {path}")
    for key, row in rows.items():
        cid = key[0]
        packet = make_packet(row["blind_id"], cases[cid], gold[cid], panel[key]["response_text"])
        require(row["adopted_judgment"] == row[row["adopted_source"]], f"Adopted judgment mismatch: {key}")
        for field in REVIEWS:
            judgment = row[field]
            if judgment is not None:
                require(judgment["blind_id"] == row["blind_id"] and judgment["packet_sha256"] == packet["packet_sha256"],
                        f"Blind packet checksum mismatch: {key}, {field}")
    return rows


def resolved(rows):
    return {key: {"judgment": row["adopted_judgment"], "pending": row["pending_metric_fields"],
                  "review_pending": False} for key, row in rows.items()}


def check_batches(panel, rows, proof, generation, scope):
    batches = proof[scope]["batches"]
    expected_size = 120 if scope == "formal" else 18
    require(Counter(row["source"]["batch_id"] for row in panel.values()) ==
            {batch["batch_id"]: expected_size for batch in batches}, f"Generation batch mismatch: {scope}")
    require({row["source"]["study_request_number"] for row in panel.values()} == set(range(1, len(panel) + 1)),
            f"Generation request numbers mismatch: {scope}")
    starts = [datetime.fromisoformat(b["started_at"]) for b in batches]
    ends = [datetime.fromisoformat(b["finished_at"]) for b in batches]
    require((max(ends) - min(starts)).total_seconds() == generation[scope]["generation_wall_clock_seconds"],
            f"Wall-clock duration mismatch: {scope}")
    for arch in (*ARCHITECTURES, "all"):
        records = [row for row in panel.values() if arch in ("all", row["architecture"])]
        summary = (generation[scope]["overall"] if arch == "all" else generation[scope]["by_architecture"][arch])
        require(all(sum(row[field] for row in records) == summary[field]["total"] for field in TOKENS),
                f"Token-use mismatch: {scope}, {arch}")
        require(abs(sum(row["latency_ms"] for row in records) / 1000 - summary["latency_seconds"]) < 0.000001,
                f"Request latency mismatch: {scope}, {arch}")
    judge_batches = load_json(FINAL / f"{scope}-review-batches.json")
    scope_counts = Counter()
    batch_for_id = {}
    for batch in judge_batches:
        for blind_id in batch["accepted_ids"]:
            require(blind_id not in batch_for_id, f"Duplicate primary reviewing unit: {scope}")
            batch_for_id[blind_id] = {field: batch[field] for field in ("package", "batch", "reported_launch")}
        launch = batch["reported_launch"]
        scope_counts[launch["model_launch"], launch["reasoning_effort_launch"]] += batch["valid"]
    require(len(batch_for_id) == len(rows) and
            all(row["primary_review_batch"] == batch_for_id[row["blind_id"]] for row in rows.values()),
            f"Primary review batch mismatch: {scope}")
    require(scope_counts == ({("gpt-6.2-astra", "high"): 108, ("gpt-6.2-astra", "medium"): 252,
                              ("gpt-6.2-sol", "xhigh"): 720} if scope == "formal"
                             else {("gpt-6.2-astra", "high"): 18}), f"Reviewer setting counts mismatch: {scope}")


def check_recorded_study(scope, records):
    config, cases, units, packets, _, _ = prepare(scope, "low")
    by_case = {case["case_id"]: case for case in cases}
    panel = index(records)
    require(panel.keys() == {unit_key(unit) for unit in units}, f"Unexpected generation units: {scope}")
    for key, row in panel.items():
        validate_response(row)
        cid, arch, _ = key
        packet = packets[cid, arch]
        require(row["status"] == "ok" and row["synthetic"] is False, f"Unexpected generation status: {key}")
        reconcile_attempt({**row, "g5_request_number": row["source"]["study_request_number"]},
                          config, by_case[cid], packet, RECORD_VERSION)
        require(row.get("adjudication_sources") == packet.get("adjudication_sources"),
                f"Different ruling sources: {key}")
    return panel, by_case


def check_excerpts(file, panel):
    for row in read_jsonl(HW5 / file):
        original = panel[unit_key(row)]
        require(row["response_text"] == original["response_text"] and row["source"] == original["source"],
                f"Excerpt differs from archive: {file}, {unit_key(row)}")


def check_original_agreement(rows, gold, expected):
    def undecided(value):
        return value is None or value in ("uncertain", "not_applicable")

    dependent = {"evidence.focal_leak": "leakage_class", "evidence.cross_player_leak": "cross_player_leak",
                 "evidence.narrative_conflict": "narrative_consistent"}
    for name, sampled in (("preplanned_stratified_sample", True), ("targeted_additional_reviews", False)):
        counts = Counter()
        for row in rows.values():
            human, primary = row["independent_human"], row["primary"]
            if human is None or row["in_random_sample"] != sampled:
                continue
            ref = gold[row["case_id"]]
            first, second = decision_signature(primary, ref), decision_signature(human, ref)
            unavailable = {key for key, value in first.items() if undecided(value) or undecided(second[key])}
            unavailable.update(row.get("closed_undetermined_fields", []))
            unavailable.update(key for key, parent in dependent.items() if parent in unavailable)
            compared = first.keys() - unavailable
            counts["paired_cases"] += 1
            if not any(needs_independent(judgment, ref) for judgment in (primary, human)):
                counts["fully_determinate_cases"] += 1
                counts["exact_fully_determinate_cases"] += first == second
            counts["compared_fields"] += len(compared)
            counts["agreeing_fields"] += sum(first[key] == second[key] for key in compared)
            counts["differing_fields"] += sum(first[key] != second[key] for key in compared)
            counts["excluded_undetermined_fields"] += len(first.keys() & unavailable)
        require(counts == Counter(expected[name]), f"Original rater agreement mismatch: {name}")


def check_formal(summary, generation, proof):
    paths = sorted((FINAL / "formal-qa-responses").glob("*.jsonl"))
    require(len(paths) == 9, "Expected nine formal batch files")
    panel, cases = check_recorded_study("formal", [row for path in paths for row in read_jsonl(path)])
    gold = load_evidence("data/g5/formal_candidate_gold.jsonl", list(cases.values()))
    rows = decision_rows(FINAL / "formal-qa-judgments.jsonl", panel, cases, gold, g5_judging.make_packet)
    computed = {(r["stratum"], r["value"], r["architecture"]): r["metrics"]
                for r in aggregate(panel, cases, gold, resolved(rows))}
    expected = {(r["stratum"], r["value"], r["architecture"]): r["metrics"]
                for r in summary["stratified_metrics"]}
    require(computed == expected, "Formal QA stratum counts differ from final scoring summary")
    require({arch: computed[("overall" if arch == "all" else "architecture", arch, arch)]
             for arch in (*ARCHITECTURES, "all")} == summary["metrics_by_architecture_and_overall"],
            "Formal QA overall counts differ from final scoring summary")
    require({key for key, row in rows.items() if row["in_random_sample"]} ==
            set(choose_sample("formal", formal_slots(list(cases.values())), panel)), "QA prespecified sample mismatch")
    check_original_agreement(rows, gold, summary["agreement_between_original_raters"])
    require(sum(row["independent_human"] is not None for row in rows.values()) == 51, "QA human review count mismatch")
    require(Counter(row["adopted_source"] for row in rows.values()) == summary["adopted_sources"],
            "QA adopted judgment sources mismatch")
    for file in ("qa-representative-answers.jsonl", "c2-reveal-failures.jsonl"):
        check_excerpts(file, panel)
    check_batches(panel, rows, proof, generation, "formal")
    audit = index(read_jsonl(HW5 / "c2-reveal-failures.jsonl"))
    failures = {}
    for key, row in panel.items():
        g = gold[key[0]]
        judgment = rows[key]["adopted_judgment"]
        if row["architecture"] != "C2" or not g["eligibility"]["R"] or judgment["reveal_result"] == "correct":
            continue
        target = set(g["required_reveal_fact_ids"])
        present = target <= set(row["input_source_fact_ids"])
        require(present == (target <= set(row["selected_fact_ids"])), f"Disagreement between retrieval and input: {key}")
        failures[key] = "generation" if present else "retrieval_miss"
    require(failures == {key: row["failure_stage"] for key, row in audit.items()}
            and Counter(failures.values()) == {"retrieval_miss": 21, "generation": 8}, "C2 failure audit mismatch")
    print("Formal QA: 1,080 responses, all strata reproduce, 29 C2 disclosure failures (21 input gaps and 8 output failures).")


def check_rp(summary, generation, proof):
    panel, _ = check_recorded_study("rp", read_jsonl(FINAL / "rp-responses.jsonl"))
    definitions = g5_rp.load_cases()
    cases = {case["case_id"]: dict(case, definition=definitions["worlds"][case["world"]])
             for case in definitions["cases"]}
    gold = {row["case_id"]: row for row in load_json(g5_rp.DATA / "reference.json")["references"]}
    rows = decision_rows(FINAL / "rp-judgments.jsonl", panel, cases, gold, g5_rp_review.make_packet)
    require(g5_rp_review.aggregate(panel, gold, resolved(rows)) == summary["metrics_by_architecture"],
            "RP counts differ from final scoring summary")
    sample = {key for key, row in rows.items() if row["in_random_sample"]}
    require(sample == set(choose_sample("rp", rp_candidates(definitions["cases"]), panel)) and
            sum(row["adjudication"] is not None for row in rows.values()) == 1, "RP review sample mismatch")
    check_excerpts("rp-narrations.jsonl", panel)
    check_batches(panel, rows, proof, generation, "rp")
    print("Role-play: 18 responses, all ruling, disclosure, narrative and retrieval counts reproduce.")


def check_pilot():
    dev_config = load_json("configs/g5-dev.json")
    cases = load_snapshots(dev_config["cases_path"], "dev")
    by_case = {case["case_id"]: case for case in cases}
    gold = load_evidence("data/g5/dev_gold.jsonl", cases)
    formal = load_snapshots("data/g5/formal_candidate_cases.jsonl", "formal_candidate")
    validate_design(cases, formal, gold, load_evidence("data/g5/formal_candidate_gold.jsonl", formal))
    panels = {}
    for effort in ("none", "low"):
        panel = index(read_jsonl(PILOT / f"{effort}-responses.jsonl"))
        require(len(panel) == 36 and all(row["status"] == "ok" and row["synthetic"] is False for row in panel.values()),
                f"Pilot response mismatch: {effort}")
        config = {**dev_config, "provider": "openai", "openai": load_json(FINAL / "archive-provenance.json")["pilot"][effort]["settings"]}
        for key, row in panel.items():
            validate_response(row)
            case = by_case[key[0]]
            packet = snapshot_packet(config, case, key[1])
            reconcile_attempt({**row, "g5_request_number": row["source"]["study_request_number"]},
                              config, case, packet, RECORD_VERSION)
        rows = decision_rows(PILOT / f"{effort}-judgments.jsonl", panel, by_case, gold, g5_judging.make_packet)
        require(sum(row["independent_human"] is not None for row in rows.values()) == 6,
                f"Pilot human review mismatch: {effort}")
        outcomes = {key: row["adopted_judgment"]["leakage_class"] for key, row in rows.items()
                    if gold[key[0]]["eligibility"]["U"]}
        panels[effort] = outcomes
        require(len(outcomes) == 18 and sum(value == "explicit" for value in outcomes.values()) ==
                (8 if effort == "none" else 4), f"Pilot leakage count mismatch: {effort}")
    require(panels["none"].keys() == panels["low"].keys(), "Pilot pairing mismatch")
    pairs = Counter((panels["none"][key], panels["low"][key]) for key in panels["none"])
    require(pairs == {("explicit", "explicit"): 4, ("explicit", "none"): 4, ("none", "none"): 10},
            "Pilot paired outcomes mismatch")
    errors = read_jsonl(PILOT / "connection-failures-responses.jsonl")
    require(len(errors) == 3 and all(r["status"] == "error" for r in errors), "Pilot error count mismatch")
    for row in errors:
        validate_response(row)
    print("Pilot: 36 pairs, 8 to 4 direct leaks, and 3 separate connection failures.")


def check_g4():
    for name in load_json(G4 / "raw-sha256.json"):
        directory = G4 / name
        rows = read_jsonl(directory / "records.jsonl")
        require(len(rows) == 24 and all(r["status"] == "ok" and r["synthetic"] is False for r in rows),
                f"G4 record count mismatch: {name}")
        require(all(sha256(r["response_text"].encode()).hexdigest() == r["response_utf8_sha256"] for r in rows),
                f"G4 response checksum mismatch: {name}")
        for arch in ARCHITECTURES:
            actual = sorted((row["turn_id"], "F003" in row["input_source_fact_ids"]) for row in rows
                            if row["architecture"] == arch)
            require([visible for _, visible in actual] == ([True] * 8 if arch != "C2" else
                    [False, False, True, True, False, False, True, True]), f"G4 disclosure trajectory mismatch: {name}, {arch}")
    print("Dynamic replay: both eight-turn runs and all 48 responses verified.")


def main():
    manifest = load_json(FINAL / "public-sha256.json")
    for name, checksum in manifest["file_sha256"].items():
        require(file_hash(name) == checksum, f"Public archive checksum mismatch: {name}")
    final = load_json(FINAL / "final-scoring-summary.json")
    generation = load_json(FINAL / "generation-summary.json")
    proof = load_json(FINAL / "archive-provenance.json")
    check_g4()
    check_pilot()
    check_formal(final["formal_qa"], generation, proof)
    check_rp(final["rp"], generation, proof)
    print("Verified public file hashes, exact request and response hashes, and reconstructed blind packets. No API calls.")


if __name__ == "__main__":
    main()
