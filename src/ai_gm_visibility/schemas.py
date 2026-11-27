"""Strict data loading; gold is deliberately loaded through a separate API."""

import json
from pathlib import Path


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def unique(items, field):
    ids = [x[field] for x in items]
    require(len(ids) == len(set(ids)), f"Duplicate {field}")
    require(all(isinstance(x, str) and x for x in ids), f"Invalid {field}")
    return set(ids)


def validate_world(world):
    require(set(world) == {"schema_version", "world_id", "revision", "actors", "entities", "facts", "reveal_rules", "source_manifest"}, "Invalid world fields")
    require(world["schema_version"] == "0.1", "Unsupported schema")
    require(type(world["revision"]) is int and world["revision"] > 0, "Invalid revision")
    actors = world["actors"]
    require(actors and len(actors) == len(set(actors)), "Invalid actors")
    require(all(isinstance(a, str) and a and a not in {"GM", "PUBLIC"} for a in actors), "Reserved actor")
    entities = unique(world["entities"], "entity_id")
    facts = unique(world["facts"], "fact_id")
    rules = unique(world["reveal_rules"], "rule_id")
    for f in world["facts"]:
        require(set(f) <= {"fact_id", "content", "entities", "location_id", "initial_readers", "reveal_rule_ids", "source_ref"}, "Unknown fact field")
        require(isinstance(f["content"], str) and bool(f["content"].strip()), "Empty fact")
        readers = f.get("initial_readers", [])
        require(readers and set(readers) <= set(actors) | {"GM", "PUBLIC"}, "Undefined readers")
        require(set(f["entities"]) <= entities, "Unknown entity")
        require(set(f.get("reveal_rule_ids", [])) <= rules, "Unknown rule")
        require(f.get("location_id") is None or f["location_id"] in entities, "Unknown location")
    for r in world["reveal_rules"]:
        require(set(r) == {"rule_id", "event_type", "target_id", "required_outcome", "grant_fact_ids", "recipient", "requires_fact_ids"}, "Invalid rule fields")
        require(r["event_type"] in {"read_document", "investigation_succeeded"}, "Invalid rule event")
        require(r["target_id"] in entities and r["recipient"] == "event_actor", "Invalid rule target/recipient")
        require(r["required_outcome"] == "success", "Unsupported outcome")
        require(r["grant_fact_ids"] and set(r["grant_fact_ids"]) <= facts, "Invalid grant reference")
        require(set(r["requires_fact_ids"]) <= facts, "Invalid prerequisite")
    return world


def load_world(path):
    try:
        return validate_world(load_json(path))
    except (KeyError, TypeError) as exc:
        raise ValidationError(f"Malformed world: {exc}") from exc


def load_cases(path, world):
    cases = load_json(path)
    unique(cases, "case_id")
    for case in cases:
        require(set(case) == {"case_id", "world_id", "split", "actor_id", "query", "query_type", "secret_type", "events_before", "turn_index", "scenario_id"}, "Invalid case fields")
        require(case["world_id"] == world["world_id"] and case["actor_id"] in world["actors"], "Case world/actor mismatch")
        require(case["split"] == "dev", "Only development cases supported")
        require(isinstance(case["query"], str) and case["query"].strip(), "Empty query")
        require(type(case["turn_index"]) is int and case["turn_index"] > 0, "Invalid turn index")
    require([c["turn_index"] for c in cases] == list(range(1, len(cases) + 1)), "Nonsequential scenario")
    require(len({c["scenario_id"] for c in cases}) == 1, "One scenario per file required")
    return cases


def load_gold(path, cases, world):
    gold = load_json(path)
    unique(gold, "case_id")
    require({g["case_id"] for g in gold} == {c["case_id"] for c in cases}, "Gold cases mismatch")
    facts = {f["fact_id"] for f in world["facts"]}
    for g in gold:
        for key in ("expected_visible_ids", "forbidden_fact_ids", "required_fact_ids", "relevant_visible_ids"):
            require(set(g[key]) <= facts, f"Unknown gold reference: {key}")
        visible = set(g["expected_visible_ids"])
        require(not visible & set(g["forbidden_fact_ids"]), "Contradictory gold")
        require(set(g["required_fact_ids"]) <= visible, "Required fact is hidden")
        require(set(g["relevant_visible_ids"]) <= visible, "Relevant fact is hidden")
        require(set(g["forbidden_fact_ids"]) == facts - visible, "Incomplete forbidden set")
    return {g["case_id"]: g for g in gold}
