"""Freeze the approved G5 study, run its two stages, and publish local blind handoffs."""

import argparse
from collections import Counter
from collections.abc import Callable
from hashlib import sha256
import hmac
import json
import os
from math import ceil
from pathlib import Path
from secrets import token_bytes
from shutil import copyfile
from zipfile import ZIP_DEFLATED, ZipFile

from ai_gm_visibility import g5_rp, g5_study
from ai_gm_visibility.g5_judging import FORMAL_RUBRIC, RUBRIC_VERSION, export_bundle, write_rows
from ai_gm_visibility.g5_panel import load_panel
from ai_gm_visibility.g5_review import evidence_context
from ai_gm_visibility.g5_rp_review import score as rp_score
from ai_gm_visibility.g5_study_sampling import choose_sample, formal_slots, rp_candidates
from ai_gm_visibility.g5_work import export_batches
from ai_gm_visibility.logging_io import digest, file_hash, read_jsonl, write_json
from ai_gm_visibility.schemas import load_json, require

ROOT = Path("runs/g5-approved-2026-11-26")
RESEARCH = ROOT / "researcher"
HANDOFF = ROOT / "blind-handoffs"
GOLD = Path("data/g5/formal_candidate_gold.jsonl")
PILOT_DIR = Path("runs/g5-pilot-2026-11-26-work-import/researcher")
PILOT_ASSESSMENT = PILOT_DIR / "human-assessment-3eb5ca6fa3bc/human-import-receipt_PRIVATE.json"
PILOT_RUNS = {"none": "20261125T193109Z-533a34da", "low": "20261125T193229Z-c3248544"}
AUTHORIZATION = "User's explicit approval in this conversation, 2026-11-26 Asia/Taipei, incorporating the 05:18 handoff"


def save_match(path, value):
    if path.exists():
        require(digest(load_json(path)) == digest(value), f"Frozen file differs: {path}")
    else:
        write_json(path, value)


def pilot_tokens():
    result = {}
    for effort, run_id in PILOT_RUNS.items():
        manifest, _, panel, provenance = load_panel(Path("runs") / run_id)
        config = manifest["config"]["openai"]
        require(config["reasoning_effort"] == effort and config["max_output_tokens"] == 2048
                and len(panel) == 36 and all(row["status"] == "ok" for row in panel.values()),
                "Pilot outputs or settings differ from the approved comparison")
        stats = {}
        for arch in ("all", "C0", "C1", "C2"):
            rows = [r for r in panel.values() if arch == "all" or r["architecture"] == arch]
            counts = {"responses": len(rows),
                      "near_cap": sum(r["output_tokens"] >= ceil(0.8 * 2048) for r in rows),
                      "truncated": sum(((r["response_metadata"] or {}).get("incomplete_details") or {}).get("reason")
                                       == "max_output_tokens" for r in rows)}
            for field in ("output_tokens", "reasoning_tokens"):
                values = sorted(r[field] for r in rows)
                counts[field] = {"max": values[-1], "p95": values[ceil(0.95 * len(values)) - 1]}
            require(counts["truncated"] == counts["near_cap"] == 0, "Pilot output ceiling requires review")
            stats[arch] = counts
        result[effort] = {"source": provenance, "by_architecture": stats}
    return result


def frozen_plan(scope):
    plan = g5_study.prepare(scope, "low", {"rp": "0.10", "formal": "4"}[scope], 2048)
    expected_requests = 18 if scope == "rp" else 1080
    effective = plan[0]["openai"]
    require(plan[-1]["requests"] == expected_requests and plan[-1]["fits_budget"] and effective == {
        "model": "gpt-6-luna", "reasoning_effort": "low", "max_output_tokens": 2048, "timeout_seconds": 60,
        "max_retries": 0, "store": False,
    }, f"Unexpected {scope} settings or budget")
    return plan


def freeze():
    RESEARCH.mkdir(parents=True, exist_ok=True)
    HANDOFF.mkdir(exist_ok=True)
    assessment = load_json(PILOT_ASSESSMENT)
    require(assessment["accepted"] == assessment["fully_agree"] == 12
            and assessment["pending_adjudications"] == 0, "Pilot human review is incomplete")
    save_match(RESEARCH / "pilot-tokens.json", pilot_tokens())
    for scope in ("rp", "formal"):
        plan = frozen_plan(scope)
        report = plan[-1]
        slots = formal_slots(plan[1]) if scope == "formal" else rp_candidates(g5_rp.load_cases()["cases"])
        require(digest(slots) == report["human_review_design_sha256"], "Sampling design changed")
        save_match(RESEARCH / f"{scope}-review-design_PRIVATE.json",
                   {"scope": scope, "plan_id": report["plan_id"], "slots_or_ranked_groups": slots})
        save_match(RESEARCH / f"{scope}-preflight.json", report)
        save_match(RESEARCH / f"{scope}-dispatch-order_PRIVATE.json", {"plan_id": report["plan_id"], "units": plan[2]})
        receipt = {"approved": True, "scope": scope, "plan_id": report["plan_id"], "approved_by": AUTHORIZATION,
                   "pilot_assessment": f"{PILOT_ASSESSMENT.as_posix()} SHA-256 {file_hash(PILOT_ASSESSMENT)}",
                   "approved_settings": report}
        if scope == "formal":
            receipt["formal_dataset_and_rubric_decisions"] = (
                "Authorized docktheatre F033, lanternferry F007, copperfeast F019 and winterqueue F047. "
                f"Cases {report['cases_sha256']}, gold {report['evidence_sha256']}, rubric {report['rubric_sha256']}.")
        g5_study.approved(plan, receipt)
        save_match(RESEARCH / f"{scope}-approval_PRIVATE.json", receipt)
        print(f"{scope}: {report['plan_id']} reserve USD {report['whole_study_reservation_usd']}", flush=True)


def archive_files(destination, directory, names):
    pending = destination.with_suffix(".part")
    with ZipFile(pending, "w", ZIP_DEFLATED) as archive:
        for name in sorted(names):
            archive.write(directory / name, name)
    with ZipFile(pending) as archive:
        require(archive.testzip() is None, f"Failed to verify {destination}")
    pending.replace(destination)


def handoff_human(destination, folder, packets, rubric):
    folder.mkdir()
    write_rows(folder / "independent_packets.jsonl", packets)
    copyfile(rubric, folder / "judge_rubric.md")
    (folder / "README.md").write_text(
        "# 獨立人工盲評\n\n請依規準逐筆閱讀，保留匿名 ID 與題包雜湊。\n"
        "請先完成獨立判定，再將同一規準的 UTF-8 JSONL 回傳研究者。\n", encoding="utf-8")
    archive_files(destination, folder, ["independent_packets.jsonl", "judge_rubric.md", "README.md"])


def dispatch(plan):
    scope = plan[-1]["scope"]
    receipt = load_json(RESEARCH / f"{scope}-approval_PRIVATE.json")
    require(load_json(RESEARCH / f"{scope}-preflight.json") == plan[-1], "Preflight changed after approval")
    require(os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY is missing")
    result = g5_study.dispatch(plan, plan[-1]["dispatch_batch_size"], approval=receipt)
    ledger = result["ledger"]
    print(json.dumps({"scope": scope, "attempted": ledger["requests_used"], "ok_in_batch": result.get("ok"),
                      "failed_in_batch": result.get("failed"), "stop": ledger["stop_reason"],
                      "cost_usd": ledger["estimated_cost_usd"]}), flush=True)
    return ledger


def run_rp():
    plan = frozen_plan("rp")
    previous = g5_study.status(plan)
    ledger = previous.get("ledger")
    if not ledger or (not ledger["stop_reason"] and ledger["requests_remaining"]):
        ledger = dispatch(plan)
    review = RESEARCH / "rp-review"
    if not review.exists():
        g5_study.collect(plan, review)
    blind = RESEARCH / "rp-blind"
    if not blind.exists():
        rp_score("export", review, blind)
    assessment = RESEARCH / "rp-independent"
    if not assessment.exists():
        rp_score("assess", review, assessment, blind)
    work = blind / "WORK_BLIND"
    archive_files(HANDOFF / "G5-RP-Work-Blind.zip", work, [p.name for p in work.iterdir()])
    packets = read_jsonl(assessment / "independent_packets.jsonl")
    human_zip = HANDOFF / "G5-RP-Human-Blind.zip"
    if not human_zip.exists():
        handoff_human(human_zip, RESEARCH / "rp-human-share", packets, blind / "judge_rubric.md")
    count = len(read_jsonl(blind / "judge_packets.jsonl"))
    save_match(RESEARCH / "rp-handoff.json", {"plan_id": plan[-1]["plan_id"], "attempted": ledger["requests_used"],
               "successful": count, "human_packets": len(packets), "stop_reason": ledger["stop_reason"]})
    print(f"RP handoff ready: {count} Work packets, {len(packets)} independent human packets", flush=True)
    require(count == 18 and not ledger["stop_reason"], "RP did not complete all 18 successfully; formal remains pending")


def formal_checkpoint(plan, ledger, blind_id_for: Callable, previous_ids, previous_sample):
    attempted = ledger["requests_used"]
    label = f"formal-{attempted:04d}"
    review, bundle = RESEARCH / f"{label}-review", RESEARCH / f"{label}-blind"
    if not review.exists():
        g5_study.collect(plan, review, checkpoint=True)
    cases, gold, panel, provenance = evidence_context(review, GOLD)
    if not bundle.exists():
        export_bundle(bundle, cases, gold, panel, provenance, rubric=FORMAL_RUBRIC, blind_id_for=blind_id_for)
    rows = read_jsonl(bundle / "judge_packets.jsonl")
    packets = {row["blind_id"]: row for row in rows}
    current_ids = set(packets)
    current_work = [row for row in rows if row["blind_id"] not in previous_ids]
    selected = choose_sample("formal", formal_slots(cases.values()), panel)
    mapping = load_json(bundle / "mapping_PRIVATE.json")["mapping"]
    sample_ids = {item["blind_id"] for item in mapping if item["blinding_masked"]
                  or (item["case_id"], item["architecture"], item["repeat_id"]) in selected}
    current_human = [row for row in rows if row["blind_id"] in sample_ids - previous_sample]
    if current_work:
        work = RESEARCH / f"{label}-work-share"
        if not work.exists():
            export_batches(work, current_work, rubric=FORMAL_RUBRIC, rubric_version=RUBRIC_VERSION)
        archive_files(HANDOFF / f"G5-{label}-Work-Blind.zip", work, [path.name for path in work.iterdir()])
    if current_human and not (HANDOFF / f"G5-{label}-Human-Blind.zip").exists():
        handoff_human(HANDOFF / f"G5-{label}-Human-Blind.zip", RESEARCH / f"{label}-human-share",
                      current_human, FORMAL_RUBRIC)
    receipt = {"plan_id": plan[-1]["plan_id"], "attempted": attempted, "successful": len(rows),
               "new_work": len(current_work), "new_human": len(current_human),
               "work_ids": sorted(current_ids), "human_ids": sorted(sample_ids), "stop_reason": ledger["stop_reason"]}
    save_match(RESEARCH / f"{label}-handoff_PRIVATE.json", receipt)
    print(f"{label}: {len(rows)} successful, {len(current_work)} new Work, {len(current_human)} new human", flush=True)
    return current_ids, sample_ids


def run_formal():
    plan = frozen_plan("formal")
    rp = load_json(RESEARCH / "rp-handoff.json")
    require(rp["successful"] == 18 and not rp["stop_reason"]
            and (HANDOFF / "G5-RP-Work-Blind.zip").is_file(), "RP must complete and be handed off first")
    key_path = RESEARCH / "formal-blind-key_PRIVATE.bin"
    if not key_path.exists():
        with key_path.open("xb") as handle:
            handle.write(token_bytes(32))
    key = key_path.read_bytes()
    require(len(key) == 32, "Invalid formal blinding key")

    def blind_id_for(unit):
        message = json.dumps((plan[-1]["plan_id"], unit), separators=(",", ":")).encode()
        return hmac.new(key, message, sha256).hexdigest()[:32]

    previous_ids, previous_sample = set(), set()
    completed = sorted(RESEARCH.glob("formal-????-handoff_PRIVATE.json"))
    if completed:
        last = load_json(completed[-1])
        previous_ids, previous_sample = set(last["work_ids"]), set(last["human_ids"])
    status = g5_study.status(plan)
    ledger = status.get("ledger")
    if ledger and ledger["requests_used"] > (last["attempted"] if completed else 0):
        previous_ids, previous_sample = formal_checkpoint(plan, ledger, blind_id_for, previous_ids, previous_sample)
    while not ledger or (ledger["requests_remaining"] and not ledger["stop_reason"]):
        ledger = dispatch(plan)
        previous_ids, previous_sample = formal_checkpoint(plan, ledger, blind_id_for, previous_ids, previous_sample)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("freeze", "rp", "formal"))
    stage = parser.parse_args().stage
    {"freeze": freeze, "rp": run_rp, "formal": run_formal}[stage]()
