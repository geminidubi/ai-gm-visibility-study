"""Blind scoring and human adjudication for RP's narration-specific rubric."""

import secrets
from collections import Counter

from .g5_judging import export_bundle, import_judgments, judgment_paths, mask, verbatim, verify_bundle, write_rows
from .g5_review import blind_judgment_summary, review_steps
from .g5_rp import DATA, VERSION, load_cases
from .g5_study import load_review
from .g5_work import export_batches, import_work, persisted_judgments
from .logging_io import digest, file_hash, read_jsonl, write_json
from .schemas import load_json, require
from .snapshot_cases import ARCHITECTURES, list_ids, objlist, text_ok

RUBRIC = DATA / "review/judge-rubric.md"
INSTRUCTIONS = DATA / "review/work-instructions.md"
RUBRIC_VERSION = "g5-rp-extension-v0.2"
COMPONENT_STATUSES = {"provided", "omitted", "contradicts", "uncertain"}
BOOL_FIELDS = {"unsupported_event", "invented_change", "cross_player_leak", "over_withholding", "narrative_consistent"}
LABELS = {"fidelity": {"consistent", "contradicts", "uncertain"},
          "leakage": {"explicit", "implicit", "none", "not_applicable", "uncertain"},
          "reveal_result": {"correct", "omitted", "incorrect", "not_applicable", "uncertain"}}
IDS = {"leaked_focal_fact_ids": "focal_forbidden_fact_ids",
       "leaked_additional_fact_ids": "additional_forbidden_fact_ids"}
FIELDS = {*BOOL_FIELDS, *LABELS, *IDS, "blind_id", "packet_sha256", "observations", "background", "evidence", "reason"}


def make_packet(blind_id, case, ref, response, *, history_speaker=True):
    visible_fields, visible_history = set(ref["visible_fields"]), set(ref["visible_history"])
    content = {"blind_id": blind_id, "rubric_version": RUBRIC_VERSION, "actor": case["actor"],
               "action": case["action"], "as_of": case["as_of"], "response": mask(response),
               "world_facts": case["definition"]["facts"],
               "visible_history": [r for r in case["definition"]["history"] if r["id"] in visible_history],
               "visible_adjudication": [r for r in case["fields"] if r["id"] in visible_fields],
               "hidden_adjudication": [r for r in case["fields"] if r["id"] not in visible_fields],
               **{k: ref[k] for k in (*IDS.values(), "forbidden_fact_ids", "other_player_fact_ids",
                                    "required_background", "required_observation_components",
                                    "required_reveal_components", "metric_applicability")}}
    return dict(content, packet_sha256=digest(content))


def validate_judgment(row, packet, raw, ref):
    require(row.keys() == FIELDS and row["blind_id"] == packet["blind_id"]
            and row["packet_sha256"] == packet["packet_sha256"], "RP judgment fields or packet identity mismatch")
    require(all(row[k] in values for k, values in LABELS.items())
            and all(row[k] is None or type(row[k]) is bool for k in BOOL_FIELDS), "Invalid RP judgment value")
    for field, ids in (("observations", set(ref["required_observation_components"])),
                       ("background", {r["fact_id"] for r in ref["required_background"]})):
        values = row[field]
        require(isinstance(values, dict) and values.keys() == ids and set(values.values()) <= COMPONENT_STATUSES,
                f"RP judgment must use all applicable {field} IDs")
    leaks = set()
    for field, reference_field in IDS.items():
        found = list_ids(row[field], field)
        require(found <= set(ref[reference_field]), "Leakage ID must be an applicable forbidden fact")
        leaks |= found
    eligibility = ref["metric_applicability"]
    require((row["leakage"] != "not_applicable") == eligibility["U"]
            and (row["reveal_result"] != "not_applicable") == eligibility["R"]
            and bool(leaks) == (row["leakage"] in {"explicit", "implicit"}), "Inconsistent leakage or reveal applicability")
    if not eligibility["X"] or row["leakage"] == "uncertain":
        require(row["cross_player_leak"] is None, "Cross-player leakage is unavailable or unresolved")
    else:
        require(row["cross_player_leak"] is bool(leaks.intersection(ref["other_player_fact_ids"])),
                "Cross-player leakage must match linked forbidden fact IDs")
    components = set(row["observations"].values()) | set(row["background"].values())
    withholding = True if components & {"omitted", "contradicts"} else None if "uncertain" in components else False
    require(row["over_withholding"] is withholding, "Withholding must agree with required observations and background")
    quotes = row["evidence"]
    adverse = bool(leaks or row["unsupported_event"] or row["invented_change"] or row["narrative_consistent"] is False
                   or row["fidelity"] == "contradicts" or row["reveal_result"] == "incorrect" or "contradicts" in components)
    require(isinstance(quotes, list) and (not adverse or quotes) and text_ok(row["reason"]),
            "RP judgments require reasoning and verbatim evidence of material mistakes or leakage")
    for quote in quotes:
        verbatim(quote, packet, raw)
    return row


def decisions(row):
    result = {k: row[k] for k in (*BOOL_FIELDS, *LABELS)}
    result.update({k: sorted(row[k]) for k in IDS})
    for field in ("observations", "background"):
        result.update({f"{field}.{key}": value for key, value in row[field].items()})
    return result


def pending(ref, row):
    if row is None:
        return set()
    return {k for k, v in decisions(row).items() if v is None or v == "uncertain"
            if k != "cross_player_leak" or ref["metric_applicability"]["X"]}


def differences(first, second, _ref):
    if first is None or second is None:
        return set()
    left, right = decisions(first), decisions(second)
    return {k for k in left if left[k] != right[k]}


def score(operation, run, output=None, bundle=None, *, inputs=None, model=None, reviewer=None, human=None,
          adjudications=None, batch_options=None):
    _, _, panel, provenance = load_review(run, "rp")
    definitions = load_cases()
    cases = {c["case_id"]: dict(c, definition=definitions["worlds"][c["world"]]) for c in definitions["cases"]}
    ref_path = DATA / "reference.json"
    document = load_json(ref_path)
    refs = objlist(document["references"], "case_id")
    require(document["rubric_version"] == RUBRIC_VERSION and refs.keys() == cases.keys(), "RP scoring dataset mismatch")
    provenance.update(dataset_version=VERSION, rubric=file_hash(RUBRIC), rubric_version=RUBRIC_VERSION,
                      reference=file_hash(ref_path))
    batch_options = {**(batch_options or {}), "rubric": RUBRIC, "rubric_version": RUBRIC_VERSION,
                     "instructions": INSTRUCTIONS}
    if operation == "export":
        count = export_bundle(output, cases, refs, panel, provenance, packet_factory=make_packet, rubric=RUBRIC)
        batches = export_batches(output / "WORK_BLIND", read_jsonl(output / "judge_packets.jsonl"), **batch_options)
        return {"blind_packets": count, "work_batches": len(batches["batches"]), "output": str(output)}
    packets, mapping, index = verify_bundle(bundle, cases, refs, panel, provenance, packet_factory=make_packet)
    result = {}
    if operation == "import-work":
        paths = judgment_paths(inputs)
        require(bool(paths), "No Work JSONL judgment files found")
        receipts = import_work(bundle, paths, model, reviewer, packets, index, panel, refs,
                               validator=validate_judgment, rubric_version=RUBRIC_VERSION)
        result = {"delivery_receipts": receipts, "import_status": "COMMITTED_WITH_REJECTIONS" if any(
            r["status"] not in {"accepted", "already_accepted"} for r in receipts) else "COMMITTED"}
    primary, origins, counts, invalid, conflicts = persisted_judgments(bundle, index, packets, panel, refs,
                                                                      validator=validate_judgment)
    missing = [bid for key, bid in index.items() if key not in primary]
    result.update(planned=len(panel), eligible_for_review=len(index), work_received_valid=len(primary), work_pending=missing,
                  work_uncertain={index[key]: sorted(pending(refs[key[0]], row)) for key, row in primary.items()
                                  if pending(refs[key[0]], row)}, import_receipt_counts=counts,
                  unresolved_import_errors=invalid, conflicting_work_blind_ids=[index[key] for key in conflicts])
    if operation == "resume-work":
        result["work_exports"] = export_batches(output, (packets[bid] for bid in missing), **batch_options)
    if operation == "assess":
        from .g5_study_sampling import RP_SAMPLE_SEED, choose_sample, rp_candidates

        independent, arbiters = (import_judgments(p, packets, mapping, panel, refs, validator=validate_judgment)
                                 for p in (human, adjudications))
        review, disputes, resolved, log, agreement = review_steps(
            index, mapping, refs, primary, independent, arbiters, conflicts, enqueue_missing=False,
            pending_for=pending, requires_review=lambda row, ref: bool(pending(ref, row)), differences=differences,
            metric_disagreements=lambda fields, ref: fields,
            random_sample=choose_sample("rp", rp_candidates(definitions["cases"]), panel),
            flagged_import_ids=(row["blind_id"] for row in invalid),
        )
        output.mkdir(parents=True, exist_ok=False)
        (output / "judge_rubric.md").write_bytes(RUBRIC.read_bytes())
        (output / "work_instructions.md").write_bytes(INSTRUCTIONS.read_bytes())
        blind_review = [packets[index[key]] for key in review]
        blind_disputes = [{"packet": packets[index[key]], "initial": blind_judgment_summary(primary.get(key)),
                           "independent": blind_judgment_summary(independent[key]),
                           "other_judgments": blind_judgment_summary(conflicts.get(key, []))} for key in disputes]
        secrets.SystemRandom().shuffle(blind_review)
        secrets.SystemRandom().shuffle(blind_disputes)
        write_rows(output / "independent_packets.jsonl", blind_review)
        write_rows(output / "adjudication_packets.jsonl", blind_disputes)
        for row in log:
            key = row["case_id"], row["architecture"], row["repeat_id"]
            row.update(work_origin=origins.get(key), conflicting_work_judgments=conflicts.get(key, []))
        write_rows(output / "decisions_PRIVATE.jsonl", log)
        result.update(independent_pending=len(review), adjudication_pending=len(disputes),
                      human_sample={"seed": RP_SAMPLE_SEED, "planned": 3, **agreement}, provenance=provenance,
                      by_architecture=aggregate(panel, refs, resolved))
        write_json(output / "assessment_PRIVATE.json", result)
    return result


def aggregate(panel, refs, resolved):
    eligibility = {"leakage": "U", "leaked_focal_fact_ids": "U_focus", "leaked_additional_fact_ids": "U_additional",
                   "cross_player_leak": "X", "reveal_result": "R", "over_withholding": "L"}
    result = {}
    for arch in ARCHITECTURES:
        counts, recall = {}, Counter()
        for key, row in panel.items():
            if key[1] != arch:
                continue
            ref = refs[key[0]]
            decision = resolved.get(key)
            fields = dict.fromkeys((*BOOL_FIELDS, *LABELS, *IDS))
            fields.update({f"observations.{fid}": None for fid in ref["required_observation_components"]})
            background = {b["fact_id"] for b in ref["required_background"]}
            fields.update({f"background.{fid}": None for fid in background})
            for field in fields:
                if field in eligibility and not ref["metric_applicability"][eligibility[field]]:
                    continue
                metric = counts.setdefault(field, Counter())
                metric["applicable"] += 1
                if row["status"] != "ok":
                    metric[row["status"]] += 1
                elif decision is None:
                    metric["missing_review"] += 1
                elif decision["review_pending"] or field in decision["pending"]:
                    metric["pending"] += 1
                else:
                    value = decisions(decision["judgment"])[field]
                    metric["determinate"] += 1
                    metric[str(bool(value)).lower() if field in IDS else str(value).lower()] += 1
            if arch == "C2":
                recall["expected"] += len(background)
                if row["retrieval_status"] == "completed":
                    recall["attempted"] += len(background)
                    recall["retrieved"] += len(background.intersection(row["selected_fact_ids"]))
        result[arch] = {"semantic": counts, "retrieval": dict(recall) if arch == "C2" else "not_applicable"}
    return result
