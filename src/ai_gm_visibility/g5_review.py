"""Stage independent human sampling and retain each adjudication decision."""

import math
import random
import secrets
from collections import Counter

from .g5_evidence import load_evidence
from .g5_judging import (
    FORMAL_RUBRIC, RUBRIC, RUBRIC_VERSION, export_bundle, import_judgments, judgment_paths, mask, verify_bundle, write_rows,
)
from .g5_metrics import APPLICABILITY, aggregate, decision_signature, pending_fields
from .g5_panel import load_panel
from .g5_work import export_batches, import_work, persisted_judgments, work_progress
from .logging_io import file_hash, read_jsonl, write_json
from .schemas import require

SAMPLE_FRACTION = 0.10
SAMPLE_SEED = 517


def sample_units(index, seed=SAMPLE_SEED):
    """Random within each architecture, with a ceil(10%) minimum per stratum."""
    rng = random.Random(seed)
    groups = {}
    for key in sorted(index):
        groups.setdefault(key[1], []).append(key)
    return {key for arch in sorted(groups)
            for key in rng.sample(groups[arch], math.ceil(len(groups[arch]) * SAMPLE_FRACTION))}


def needs_independent(judgment, gold):
    return (bool(pending_fields(gold, judgment)) or judgment is None
            or judgment["evidence"]["other_private_leak"] is None
            or any(row["outcome"] == "uncertain" for row in judgment["required"]))


def compare(initial, human, gold):
    if initial is None or human is None:
        return set()
    left, right = (decision_signature(j, gold) for j in (initial, human))
    return {field for field in left if left[field] != right[field]}


def compare_required_disagreements(disagreements, gold):
    resolved = disagreements & APPLICABILITY.keys()
    if disagreements.intersection(gold["required_fact_ids"]):
        resolved.add("over_withholding")
    if disagreements.intersection(gold["required_reveal_fact_ids"]):
        resolved.add("reveal_result")
    if "evidence.focal_leak" in disagreements:
        resolved.add("leakage_class")
    if "evidence.cross_player_leak" in disagreements:
        resolved.add("cross_player_leak")
    if "evidence.narrative_conflict" in disagreements:
        resolved.add("narrative_consistent")
    if not gold["eligibility"]["X"]:
        resolved.discard("cross_player_leak")
    return resolved


def review_steps(index, mapping, gold, primary, humans, arbiters, external_conflicts=None, *, enqueue_missing=True,
                 pending_for=pending_fields, requires_review=needs_independent, differences=compare,
                 metric_disagreements=compare_required_disagreements, random_sample=None, flagged_import_ids=()):
    random_sample = sample_units(index) if random_sample is None else random_sample
    flagged_import_ids = set(flagged_import_ids)
    external_conflicts = external_conflicts or {}
    independent, adjudication, resolved, log = {}, {}, {}, []
    agreement = Counter(random_selected=len(random_sample))
    for key, bid in index.items():
        g = gold[key[0]]
        first, human, final = (source.get(key) for source in (primary, humans, arbiters))
        reasons = set()
        if key in random_sample:
            reasons.add("random_sample")
        if mapping[bid]["blinding_masked"]:
            reasons.add("architecture_marker_masked")
        if bid in flagged_import_ids:
            reasons.add("unresolved_import_error")
        if first is None:
            reasons.add("primary_missing")
        elif requires_review(first, g):
            reasons.add("primary_unresolved")

        disagreements = differences(first, human, g)
        for alternative in external_conflicts.get(key, []):
            reasons.add("conflicting_work_submission")
            disagreements |= differences(first, alternative, g)
        if disagreements:
            reasons.add("disagreement")
        if human is not None and requires_review(human, g):
            reasons.add("human_unresolved")
        if key in random_sample and first is not None and human is not None:
            agreement["random_double_scored"] += 1
            if not requires_review(first, g) and not requires_review(human, g):
                agreement["random_determinate_pairs"] += 1
                agreement["random_exact_agreement"] += not disagreements

        independent_reasons = reasons if enqueue_missing else reasons - {"primary_missing"}
        if independent_reasons and human is None:
            independent[key] = reasons
        escalation = human is not None and (
            bool(disagreements) or key in external_conflicts
            or requires_review(human, g) or (first is not None and requires_review(first, g))
        )
        require(final is None or escalation, "Arbitration requires independent judgment and a disagreement or unresolved decision")
        if escalation and (final is None or requires_review(final, g)):
            adjudication[key] = reasons | {"needs_adjudication"}

        adopted = final if final is not None else human if human is not None else first
        source = "adjudication" if final is not None else "independent_human" if human is not None else "primary" if first else None
        pending = pending_for(g, adopted)
        if final is None and escalation:
            if first is not None:
                pending |= pending_for(g, first)
            pending |= metric_disagreements(disagreements, g)
        if adopted:
            resolved[key] = dict(judgment=adopted, pending=pending,
                                 review_pending=key in independent or key in adjudication)
        log.append({
            "blind_id": bid, "case_id": key[0], "architecture": key[1], "repeat_id": key[2],
            "in_random_sample": key in random_sample, "review_reasons": sorted(reasons),
            "disagreements": sorted(disagreements), "pending_metric_fields": sorted(pending),
            "primary": first, "independent_human": human, "adjudication": final,
            "adopted_source": source, "adopted_judgment": adopted,
            "review_status": "independent_pending" if key in independent else
                             "adjudication_pending" if key in adjudication else
                             "primary_pending" if adopted is None else "resolved",
        })
    return independent, adjudication, resolved, log, agreement


def evidence_context(run_dir, gold_path):
    from .g5_study import REVIEW_FILE, load_review

    if (run_dir / REVIEW_FILE).is_file():
        manifest, rows, panel, provenance = load_review(run_dir, "formal")
        require(file_hash(gold_path) == file_hash("data/g5/formal_candidate_gold.jsonl"),
                "Use the frozen formal answer file")
    else:
        manifest, rows, panel, provenance = load_panel(run_dir)
    gold = load_evidence(gold_path, rows)
    provenance.update(
        dataset_version=manifest["config"]["dataset_version"], cases=file_hash(manifest["config"]["cases_path"]),
        gold=file_hash(gold_path), rubric=file_hash(rubric_for(provenance)), rubric_version=RUBRIC_VERSION,
    )
    return {row["case_id"]: row for row in rows}, gold, panel, provenance


def rubric_for(provenance):
    return FORMAL_RUBRIC if "study_snapshot_sha256" in provenance else RUBRIC


def export_run(run_dir, gold_path, output, *, blind_id_for=None, **batch_options):
    cases, gold, panel, provenance = evidence_context(run_dir, gold_path)
    rubric = rubric_for(provenance)
    count = export_bundle(output, cases, gold, panel, provenance, rubric=rubric, blind_id_for=blind_id_for)
    batches = export_batches(output / "WORK_BLIND", read_jsonl(output / "judge_packets.jsonl"),
                             rubric=rubric, **batch_options)
    return {"blind_packets": count, "planned": len(panel), "output": str(output),
            "work_handoff": str(output / "WORK_BLIND"), "work_batches": len(batches["batches"])}


def work_operation(operation, run_dir, gold_path, bundle_dir, *, output=None, inputs=(), model=None,
                   reviewer=None, batch_options=None):
    cases, gold, panel, provenance = evidence_context(run_dir, gold_path)
    packets, _, index = verify_bundle(bundle_dir, cases, gold, panel, provenance)
    receipts = []
    if operation == "import-work":
        paths = judgment_paths(inputs)
        require(bool(paths), "No Work JSONL judgment files found")
        receipts = import_work(bundle_dir, paths, model, reviewer, packets, index, panel, gold)
    primary, _, counts, invalid, conflicts = persisted_judgments(bundle_dir, index, packets, panel, gold)
    progress = work_progress(index, primary, panel, gold, counts, invalid, conflicts)
    if operation == "resume-work":
        missing = set(progress["work_pending_blind_ids"])
        progress["work_exports"] = export_batches(
            output, (packet for bid, packet in packets.items() if bid in missing), **(batch_options or {}),
            rubric=rubric_for(provenance),
        )
    if operation == "import-work":
        progress["delivery_receipts"] = receipts
        progress["import_status"] = ("COMMITTED_WITH_REJECTIONS" if any(
            row["status"] not in {"accepted", "already_accepted"} for row in receipts
        ) else "COMMITTED")
    return progress


def blind_judgment_summary(judgment):
    if isinstance(judgment, str):
        return mask(judgment)
    if isinstance(judgment, dict):
        return {k: blind_judgment_summary(v) for k, v in judgment.items()}
    if isinstance(judgment, list):
        return [blind_judgment_summary(v) for v in judgment]
    return judgment


def assess_run(run_dir, gold_path, bundle_dir, output, *, primary=None, human=None, adjudications=None):
    cases, gold, panel, provenance = evidence_context(run_dir, gold_path)
    packets, mapping, index = verify_bundle(bundle_dir, cases, gold, panel, provenance)
    work_rows, work_origins, receipts, invalid, conflicts = persisted_judgments(bundle_dir, index, packets, panel, gold)
    require(primary is None or not work_rows, "Use stored Work results or a manual fixture, not both")
    imported_primary, human_rows, final_rows = (
        import_judgments(path, packets, mapping, panel, gold) for path in (primary, human, adjudications)
    )
    primary_rows = work_rows or imported_primary
    sample_design = {"design": "within_architecture", "seed": SAMPLE_SEED, "minimum": SAMPLE_FRACTION}
    random_sample = None
    if "study_snapshot_sha256" in provenance:
        from .g5_study_sampling import FORMAL_SAMPLE_SEED, choose_sample, formal_slots

        random_sample = choose_sample("formal", formal_slots(cases.values()), panel)
        sample_design = {"design": "one_per_family_and_architecture", "seed": FORMAL_SAMPLE_SEED, "planned": 45}
    independent, disputes, resolved, log, agreement = review_steps(
        index, mapping, gold, primary_rows, human_rows, final_rows, conflicts, enqueue_missing=primary is not None,
        random_sample=random_sample, flagged_import_ids=(row["blind_id"] for row in invalid),
    )
    output.mkdir(parents=True, exist_ok=False)
    indep_packets = [packets[index[key]] for key in independent]
    dispute_packets = [
        dict(packet=packets[index[key]], initial=blind_judgment_summary(primary_rows.get(key)),
             independent=blind_judgment_summary(human_rows[key]),
             other_judgments=blind_judgment_summary(conflicts.get(key, [])))
        for key in disputes
    ]
    secrets.SystemRandom().shuffle(indep_packets)
    secrets.SystemRandom().shuffle(dispute_packets)
    write_rows(output / "independent_packets.jsonl", indep_packets)
    write_rows(output / "adjudication_packets.jsonl", dispute_packets)
    (output / "judge_rubric.md").write_bytes(rubric_for(provenance).read_bytes())
    for row in log:
        key = row["case_id"], row["architecture"], row["repeat_id"]
        row.update(work_origin=work_origins.get(key), conflicting_work_judgments=conflicts.get(key, []))
    write_rows(output / "decisions_PRIVATE.jsonl", log)
    write_rows(output / "adopted_units_PRIVATE.jsonl", (
        {field: record.get(field) for field in ("case_id", "world_id", "family_id", "pair_id", "architecture", "repeat_id",
          "attempt_id", "attempt_count", "status", "error_type", "retrieval_status", "selected_fact_ids",
          "adopted_record_sha256")}
        for record in panel.values()
    ))
    judgment_count = sum(map(len, (primary_rows, human_rows, final_rows)))
    status = ("NOT_RUN" if not judgment_count else
              "INCOMPLETE" if independent or disputes or len(resolved) != len(panel) else
              "COMPLETE")
    summary = {
        "semantic_status": status, "synthetic": provenance["synthetic"], "formal_result": provenance["formal_result"],
        "planned": len(panel), "primary_received": len(primary_rows), "independent_received": len(human_rows),
        "adjudications_received": len(final_rows), "independent_pending": len(independent),
        "adjudication_pending": len(disputes), "disagreement_cases": sum(bool(row["disagreements"]) for row in log),
        "random_sample": {**sample_design,
                          **{key: agreement[key] for key in ("random_selected", "random_double_scored",
                                                           "random_determinate_pairs", "random_exact_agreement")}},
        "work": work_progress(index, work_rows, panel, gold, receipts, invalid, conflicts),
        "provenance": {**provenance, "private_mapping": file_hash(bundle_dir / "mapping_PRIVATE.json"),
                       "judgments": {
                           name: [{"file": path.name, "sha256": file_hash(path)} for path in judgment_paths(inputs)]
                           for name, inputs in (("primary", primary), ("independent", human), ("adjudication", adjudications))
                           if inputs is not None}},
        "aggregates": aggregate(panel, cases, gold, resolved),
    }
    write_json(output / "assessment_PRIVATE.json", summary)
    return {key: summary[key] for key in ("semantic_status", "planned", "primary_received", "independent_pending",
                                        "adjudication_pending", "disagreement_cases")}

