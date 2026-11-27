"""Evaluation-side validation of the G5 dataset and its scoring evidence."""

from collections import Counter

from .logging_io import read_jsonl
from .schemas import require
from .snapshot_cases import DATASET_VERSION, list_ids, objlist, projection, text_ok

SECRET_TYPES = {"npc_identity", "trap", "secret_door", "other_player_private", "unrevealed_event"}
QUERY_TYPES = {"general", "direct", "indirect", "false_premise"}
DIFFICULTIES = {"easy", "medium", "hard"}
AUTHORIZATIONS = {"authorized", "unauthorized"}


def validate_gold(cases, rows):
    case_map = objlist(cases, "case_id")
    gold = objlist(rows, "case_id")
    require(case_map.keys() == gold.keys(), "Case and gold IDs differ")
    for cid, case in case_map.items():
        g = gold[cid]
        facts = objlist(case["facts"], "fact_id")
        active, visible, history = projection(case)
        ids = {field: list_ids(g[field], f"{cid}/{field}") for field in (
            "expected_visible_ids", "forbidden_fact_ids", "required_fact_ids",
            "required_public_fact_ids", "required_reveal_fact_ids", "relevant_visible_ids",
            "cross_player_fact_ids", "focal_forbidden_fact_ids", "diagnostic_private_fact_ids",
        )}
        require(g["dataset_version"] == case["dataset_version"] == DATASET_VERSION, f"{cid}: evidence version")
        require(ids["expected_visible_ids"] == visible and ids["forbidden_fact_ids"] == active - visible,
                f"{cid}: gold visibility mismatch")
        public, reveal = ids["required_public_fact_ids"], ids["required_reveal_fact_ids"]
        target = g["focal_fact_id"]
        authorized = case["authorization"] == "authorized"
        focal = set() if authorized else {target}
        cross = focal if case["secret_type"] == "other_player_private" else set()
        other_private = {fid for fid in active - visible - focal if "PLAYER_B" in facts[fid]["readers"]}
        require(public and public <= visible and all("PUBLIC" in facts[fid]["readers"] for fid in public)
                and target in active and "PUBLIC" not in facts[target]["readers"]
                and reveal == ({target} if authorized else set()) and reveal <= visible
                and ids["required_fact_ids"] == ids["relevant_visible_ids"] == public | reveal,
                f"{cid}: inconsistent gold access or required facts")
        require(ids["focal_forbidden_fact_ids"] == focal <= active - visible
                and ids["cross_player_fact_ids"] == cross
                and all("PLAYER_B" in facts[fid]["readers"] for fid in cross)
                and ids["diagnostic_private_fact_ids"] == other_private,
                f"{cid}: inconsistent forbidden or cross-player evidence")
        eligibility = g["eligibility"]
        require(set(eligibility) == set("URLX") and all(type(flag) is bool for flag in eligibility.values())
                and eligibility == {"U": not authorized, "R": authorized, "L": True, "X": bool(cross)},
                f"{cid}: wrong metric applicability")
        require(text_ok(g["reference_answer"]), f"{cid}: missing reference answer")
        legal = visible | {fid for h in history for fid in h.source_fact_ids}
        inferences = objlist(g["allowed_inferences"], "fact_id")
        require(set(inferences) <= legal and all(text_ok(row["scope"]) for row in inferences.values()),
                f"{cid}: inference has no authorized support")
    return gold


def validate_design(dev, formal, dev_gold, formal_gold):
    require(len(dev) == 12 and len(formal) == 120, "G5 split sizes")
    fields = ("case_id", "world_id", "family_id", "pair_id")
    for field in fields:
        require({c[field] for c in dev}.isdisjoint(c[field] for c in formal), f"Split overlap: {field}")
    by_pair = {}
    for case in dev + formal:
        by_pair.setdefault(case["pair_id"], []).append(case)
    require(len(by_pair) == 66, "Expected 66 authorization pairs")
    for pair, members in by_pair.items():
        by_auth = {c["authorization"]: c for c in members}
        require(len(members) == 2 and set(by_auth) == AUTHORIZATIONS, f"{pair}: broken pair")
        denied, allowed = (by_auth[auth] for auth in ("unauthorized", "authorized"))
        excluded = {"case_id", "authorization", "facts"}
        require({k: v for k, v in denied.items() if k not in excluded}
                == {k: v for k, v in allowed.items() if k not in excluded}, f"{pair}: metadata or history changed")
        target = (dev_gold if denied["split"] == "dev" else formal_gold)[denied["case_id"]]["focal_fact_id"]
        expected = [
            dict(f, readers=sorted(set(f["readers"]) | {allowed["actor_id"]})) if f["fact_id"] == target else f
            for f in denied["facts"]
        ]
        require(allowed["facts"] == expected, f"{pair}: facts changed beyond focal authorization")
    cells = {(c["secret_type"], c["query_type"], c["difficulty"], c["authorization"]) for c in formal}
    require(len(cells) == 120 and {c[0] for c in cells} == SECRET_TYPES
            and {c[1] for c in cells} == QUERY_TYPES and {c[2] for c in cells} == DIFFICULTIES,
            "Formal factorial coverage")
    counts = Counter(c["family_id"] for c in formal)
    require(len(counts) == 15 and set(counts.values()) == {8}, "Formal family coverage")
    families = {}
    for case in formal:
        families.setdefault((case["secret_type"], case["difficulty"]), set()).add(case["family_id"])
    require(all(len(names) == 1 for names in families.values()), "Formal type × difficulty family assignment")
    return {"dev_cases": len(dev), "formal_candidate_cases": len(formal), "formal_factorial_cells": len(cells),
            "dev_families": len({c["family_id"] for c in dev}), "formal_families": len(counts),
            "authorization_pairs": len(by_pair), "formal_pairs": 60}


def load_evidence(path, cases):
    return validate_gold(cases, read_jsonl(path))
