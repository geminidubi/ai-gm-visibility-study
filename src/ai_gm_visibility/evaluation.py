"""Deterministic diagnostics only. Semantic scores remain NOT_RUN."""

from .schemas import require


def score_automatic(record, gold):
    require(record["case_id"] == gold["case_id"], "Gold/record mismatch")
    relevant = set(gold["relevant_visible_ids"])
    sources = set(record["input_source_fact_ids"])
    selected = set(record["selected_fact_ids"])
    visible = set(gold["expected_visible_ids"])
    return {
        "case_id": record["case_id"], "architecture": record["architecture"],
        "attempt_id": record["attempt_id"], "status": record["status"],
        "state_matches_gold": set(record["visible_fact_ids"]) == visible,
        "input_forbidden_ids": sorted(sources & set(gold["forbidden_fact_ids"])),
        "c2_policy_ok": sources <= visible if record["architecture"] == "C2" else None,
        "retrieval_recall": len(selected & relevant) / len(relevant) if relevant else None,
        "semantic_status": "NOT_RUN", "synthetic": record["synthetic"],
    }
