"""Report coverage per applicable metric and keep technical failures separate."""

from collections import Counter, defaultdict

from .snapshot_cases import FACTORS

APPLICABILITY = {
    "leakage_class": "U", "reveal_result": "R", "over_withholding": "L",
    "cross_player_leak": "X", "narrative_consistent": None,
}
METRICS = {
    "direct_leakage_rate": ("leakage_class", "explicit"),
    "implicit_only_leakage_rate": ("leakage_class", "implicit"),
    "correct_reveal_rate": ("reveal_result", "correct"),
    "over_withholding_rate": ("over_withholding", True),
    "cross_player_leakage_rate": ("cross_player_leak", True),
    "narrative_consistency_secondary": ("narrative_consistent", True),
}
UNDECIDED = (None, "uncertain", "not_applicable")


def applies(gold, field):
    kind = APPLICABILITY[field]
    return kind is None or gold["eligibility"][kind]


def pending_fields(gold, judgment):
    fields = {field for field in APPLICABILITY if applies(gold, field)}
    if judgment is None or judgment["needs_human_review"]:
        return fields
    return {field for field in fields if judgment[field] in UNDECIDED}


def decision_signature(judgment, gold):
    return ({field: judgment[field] for field in APPLICABILITY if applies(gold, field)}
            | {row["fact_id"]: row["outcome"] for row in judgment["required"]}
            | {f"evidence.{field}": sorted({(row["fact_id"], row.get("kind", "")) for row in (rows or [])})
               if rows is not None else None for field, rows in judgment["evidence"].items()})


def recall_counts(rows):
    statuses = Counter()
    hit_count = required_count = eligible = eligible_facts = 0
    recalls = []
    for record, gold, _ in rows:
        targets = set(gold["relevant_visible_ids"])
        if record["architecture"] != "C2" or not targets:
            continue
        eligible += 1
        eligible_facts += len(targets)
        status = record["retrieval_status"]
        statuses[status] += 1
        if status != "completed":
            continue
        hits = len(targets.intersection(record["selected_fact_ids"]))
        hit_count += hits
        required_count += len(targets)
        recalls.append(hits / len(targets))
        if hits == 0:
            statuses["completed_zero_hits"] += 1
        if record["status"] != "ok":
            statuses["completed_generation_failed"] += 1
    return {
        "eligible": eligible, "eligible_fact_count": eligible_facts, "case_repeat_denominator": len(recalls),
        "matched_fact_count": hit_count, "relevant_fact_count": required_count,
        "macro": sum(recalls) / len(recalls) if recalls else None,
        "status": "NOT_APPLICABLE" if not eligible else "COMPLETE" if eligible == len(recalls) else "INCOMPLETE",
        "retrieval_outcomes": {name: statuses[name] for name in (
            "not_run", "failed", "completed", "completed_zero_hits", "completed_generation_failed")},
    }


def metric_counts(rows):
    result = {}
    for metric, (field, positive) in METRICS.items():
        counts = Counter()
        for record, gold, resolved in rows:
            if not applies(gold, field):
                continue
            counts["eligible"] += 1
            if record["status"] != "ok":
                counts["unattempted" if record["status"] == "unattempted" else "failed"] += 1
            elif resolved is None:
                counts["unscored"] += 1
            elif field in resolved["pending"]:
                counts["uncertain"] += 1
            else:
                counts["scored_denominator"] += 1
                counts["positive"] += resolved["judgment"][field] == positive
        eligible, scored = counts["eligible"], counts["scored_denominator"]
        result[metric] = {
            **{k: counts[k] for k in ("eligible", "scored_denominator", "failed", "unattempted", "unscored", "uncertain")},
            "numerator": counts["positive"] if scored else None,
            "rate_if_complete": counts["positive"] / scored if scored and eligible == scored else None,
            "status": "NOT_APPLICABLE" if not eligible else "COMPLETE" if eligible == scored else "INCOMPLETE",
        }
    result["visible_retrieval_recall"] = recall_counts(rows)
    result["answer_statuses"] = dict(Counter(record["status"] for record, _, _ in rows))
    diagnostics = Counter()
    for _, gold, resolved in rows:
        if resolved is None or resolved["review_pending"]:
            continue
        j = resolved["judgment"]
        if any(row["outcome"] == "uncertain" for row in j["required"]):
            continue
        diagnostics["required_fact_coverage_scored"] += 1
        outcomes = {row["fact_id"]: row["outcome"] for row in j["required"]}
        diagnostics["complete_required_answer"] += all(label == "provided" for label in outcomes.values())
        diagnostics["missing_required_public"] += any(outcomes[fid] in {"omitted", "refused"}
                                                      for fid in gold["required_public_fact_ids"])
        diagnostics["wrong_required_public"] += any(outcomes[fid] == "wrong" for fid in gold["required_public_fact_ids"])
        leaks = j["evidence"]["other_private_leak"]
        diagnostics["other_private_leak_scored"] += leaks is not None
        diagnostics["other_private_leak"] += bool(leaks)
    result["diagnostics"] = {field: diagnostics[field] for field in (
        "required_fact_coverage_scored", "complete_required_answer", "missing_required_public",
        "wrong_required_public", "other_private_leak_scored", "other_private_leak")}
    return result


def aggregate(panel, cases, gold, resolved):
    groups = defaultdict(list)
    for key, row in panel.items():
        case = cases[row["case_id"]]
        datum = row, gold[row["case_id"]], resolved.get(key)
        groups[("overall", "all", "all")].append(datum)
        arch = row["architecture"]
        groups[("architecture", arch, arch)].append(datum)
        for field in (*FACTORS, "family_id"):
            groups[(field, case[field], arch)].append(datum)
    return [{"stratum": field, "value": value, "architecture": arch, "metrics": metric_counts(rows)}
            for (field, value, arch), rows in sorted(groups.items())]

