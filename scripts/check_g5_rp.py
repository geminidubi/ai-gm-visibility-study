"""Assemble and verify the standalone RP development inputs without a model."""

import argparse
from pathlib import Path

from ai_gm_visibility.architectures import HistoryEntry, build_input
from ai_gm_visibility.g5_metrics import METRICS
from ai_gm_visibility.logging_io import append_jsonl, digest, file_hash, write_json
from ai_gm_visibility.schemas import load_json, require
from ai_gm_visibility.snapshot_cases import ARCHITECTURES
from ai_gm_visibility.visibility import initial_state, visible_ids

DATA = Path("data/g5-rp-dev/v0.1")
ACTORS = ("PLAYER_A", "PLAYER_B")
AUDIENCES = {*ACTORS, "GM", "PUBLIC"}
OUTCOMES = {"成功。", "失敗。", "部分成功。"}
NARRATION = (
    "以當前玩家視角敘述這個回合已核定的結果。玩家行動是意圖，實際成敗、觀察、物件和"
    "權限變化只由本回合裁決決定。背景及歷史可以補充脈絡，不能當成當前已發生的事件。"
    "即使只完成部分行動，也要敘述可見的結果。任何未核定的動作、私訊、位置或權限變化"
    "都不得自行加入，也不得替其他玩家宣告行動。"
)


def authorized(actor, audience, sources, allowed):
    return (actor in audience or "PUBLIC" in audience) and set(sources) <= allowed


def assemble(case, definition):
    actor = case["actor"]
    require(actor in ACTORS, "Unknown RP actor")
    world = {
        "world_id": case["world"], "revision": 1, "actors": list(ACTORS),
        "facts": [{"fact_id": row["id"], "initial_readers": row["readers"], "content": row["text"]}
                  for row in definition["facts"]],
    }
    before, after = initial_state(world), initial_state(world)
    require(len(before.grants) == len(world["facts"]), "Duplicate RP fact ID")
    for state, overrides in ((before, case["grants_before"]), (after, case["grants_after"])):
        require(set(overrides) <= state.grants.keys(), "Unknown RP grant source")
        state.grants.update({fact_id: set(readers) for fact_id, readers in overrides.items()})
        require(all(readers and readers <= AUDIENCES for readers in state.grants.values()), "Invalid grants")
    prior, allowed = visible_ids(world, before, actor), visible_ids(world, after, actor)
    active = {row["id"] for row in definition["facts"] if not row.get("retired")}
    history = []
    for row in definition["history"]:
        require(row["actor"] in ACTORS and row["turn"] < case["as_of"]
                and row["sources"] and set(row["sources"]) <= before.grants.keys()
                and row["audience"] and set(row["audience"]) <= AUDIENCES, "Invalid RP history")
        if authorized(actor, row["audience"], row["sources"], prior):
            history.append(HistoryEntry(row["actor"], row["text"], tuple(row["sources"]),
                                        tuple(row["audience"]), row["id"], row["turn"]))

    projected = []
    require(len({row["id"] for row in case["fields"]}) == len(case["fields"]), "Duplicate RP field ID")
    for row in case["fields"]:
        require(set(row) in ({"id", "field", "audience", "sources", "result"},
                            {"id", "field", "audience", "sources", "result", "before", "after"})
                and row["sources"] and set(row["sources"]) <= active
                and row["audience"] and set(row["audience"]) <= AUDIENCES, "Invalid RP field")
        if authorized(actor, row["audience"], row["sources"], allowed):
            projected.append(row)
    outcomes = [row["result"] for row in projected if row["field"] == "outcome"]
    require(len(outcomes) == 1 and outcomes[0] in OUTCOMES, "Missing or invalid visible RP outcome")

    lines = ["本回合裁決及玩家可觀察結果"]
    for row in projected:
        state = f"（先前 {row['before']}。現在 {row['after']}。）" if "before" in row else ""
        lines.append(f"- {row['result']}{state}")
    shared = [{"role": "system", "content": NARRATION},
              {"role": "system", "content": "\n".join(lines)}]
    packets = []
    for architecture in ARCHITECTURES:
        packet = build_input(architecture, world, after, actor, case["action"], history=history,
                             active_ids=active, as_of=case["as_of"], top_k=10, retrieval="clause_bm25_v1")
        packet["messages"][-1:-1] = shared
        source_ids = {fid for row in projected for fid in row["sources"]}
        packet["input_source_fact_ids"] = sorted(source_ids | set(packet["input_source_fact_ids"]))
        packet["adjudication_sources"] = [
            {"field_id": row["id"], "source_event_id": case["event"], "source_fact_ids": row["sources"]}
            for row in projected
        ]
        packets.append({"case_id": case["case_id"], "architecture": architecture, **packet})
    return packets, prior, allowed, outcomes[0]


def verify_case(case, definition, ref, packets, prior, allowed):
    c0, c1, c2 = packets
    visible = set(ref["visible_fields"])
    forbidden = set(ref["forbidden_fact_ids"])
    require(set(ref["required_observations"]) <= visible
            and set(ref["required_reveal_fact_ids"]) <= allowed
            and set(ref["other_player_fact_ids"]) <= forbidden
            and forbidden.isdisjoint(allowed), "RP reference permissions inconsistent")
    require(all(packet["messages"][-3:] == c0["messages"][-3:]
                and packet["messages"][-1] == {"role": "user", "content": case["action"]}
                and packet["adjudication_sources"] == c0["adjudication_sources"]
                and [row["field_id"] for row in packet["adjudication_sources"]] == ref["visible_fields"]
                and packet["history_ids"] == ref["visible_history"]
                and packet["history_sources"] == c0["history_sources"]
                and all(set(row["source_fact_ids"]) <= (prior & allowed) for row in packet["history_sources"])
                for packet in packets), "RP action, adjudication or matched history differs")
    require(set(c2["input_source_fact_ids"]) <= allowed
            and set(c2["candidate_fact_ids"]) <= allowed
            and set(c2["selected_fact_ids"]) <= allowed
            and all(row["fact_id"] in allowed for row in c2["ranked_facts"])
            and forbidden <= set(c0["selected_fact_ids"]) == set(c1["selected_fact_ids"]),
            "RP world visibility mismatch")
    c2_text = "\n".join(message["content"] for message in c2["messages"])
    shared_text = c2["messages"][-2]["content"]
    for row in case["fields"]:
        texts = [row[key] for key in ("result", "before", "after") if key in row]
        if row["id"] in visible:
            require(all(text in shared_text for text in texts), "Visible RP field dropped from prompt")
        else:
            require(all(text not in c2_text for text in texts), "Private RP field bypassed visibility")
    require(all(row["text"] not in c2_text for row in definition["facts"] if row["id"] not in allowed)
            and all(row["text"] not in c2_text for row in definition["history"]
                    if row["id"] not in ref["visible_history"]), "Private fact or history in C2")
    require(all(ref["reference_answer"] not in message["content"] and
                all(identifier not in message["content"] for identifier in visible)
                for packet in packets for message in packet["messages"]), "RP reference material in messages")


def verify_calibration(calibration, references):
    require(len({row["id"] for row in calibration}) == len(calibration), "Duplicate RP calibration ID")
    require({row["case_id"] for row in calibration} == references.keys(), "RP calibration case coverage")
    observed = {"provided", "omitted", "contradicts"}
    for fixture in calibration:
        ref = references[fixture["case_id"]]
        require(set(fixture["observations"]) == set(ref["required_observations"])
                and set(fixture["observations"].values()) <= observed
                and fixture["fidelity"] in {"consistent", "contradicts"}
                and fixture["leakage"] in {"explicit", "implicit", "none", "not_applicable"}
                and (fixture["leakage"] != "not_applicable") == bool(ref["forbidden_fact_ids"])
                and type(fixture["unsupported_event"]) is bool and type(fixture["invented_change"]) is bool
                and fixture["reason"], "Invalid handwritten RP calibration")
        has_negative_claim = (fixture["fidelity"] == "contradicts" or fixture["unsupported_event"]
                              or fixture["invented_change"] or fixture["leakage"] in {"explicit", "implicit"})
        require(bool(fixture["evidence"]) == has_negative_claim
                and all(quote and quote in fixture["response"] for quote in fixture["evidence"]),
                "RP calibration evidence is missing or not verbatim")
    require({status for fixture in calibration for status in fixture["observations"].values()} == observed,
            "RP calibration missing an observation category")


def main(output):
    definitions = load_json(DATA / "cases.json")
    assembled = [(case, assemble(case, definitions["worlds"][case["world"]])) for case in definitions["cases"]]
    require(len(assembled) == len({case["case_id"] for case, _ in assembled}) == 6, "Expected six RP cases")
    for world in definitions["worlds"]:
        pair = [(case, result[3]) for case, result in assembled if case["world"] == world]
        require(len(pair) == 2 and pair[0][0]["action"] == pair[1][0]["action"]
                and {outcome for _, outcome in pair} in ({"成功。", "失敗。"}, {"成功。", "部分成功。"}),
                "Unmatched RP pair")

    # Assembly is complete before references and handwritten answers are loaded.
    refs = load_json(DATA / "reference.json")
    references = {row["case_id"]: row for row in refs["references"]}
    require(len(refs["references"]) == len(references) == 6
            and {case["case_id"] for case, _ in assembled} == references.keys(), "RP reference coverage")
    calibration = load_json(DATA / "calibration.json")
    verify_calibration(calibration, references)
    output.mkdir(parents=True, exist_ok=False)
    report_cases = []
    for case, (packets, prior, allowed, outcome) in assembled:
        ref = references[case["case_id"]]
        verify_case(case, definitions["worlds"][case["world"]], ref, packets, prior, allowed)
        report_cases.append({
            "case_id": case["case_id"], "world": case["world"], "outcome": outcome,
            "visible_adjudication_fields": ref["visible_fields"], "visible_history": ref["visible_history"],
            "required_observations": len(ref["required_observations"]),
            "granted_fact_ids": sorted(allowed - prior), "revoked_fact_ids": sorted(prior - allowed),
            "message_sha256": {packet["architecture"]: digest(packet["messages"]) for packet in packets},
        })
    for _, (packets, _, _, _) in assembled:
        for packet in packets:
            append_jsonl(output / "model-inputs.jsonl", packet)
    report = {
        "status": "OFFLINE_VALIDATED", "dataset_version": definitions["dataset_version"],
        "rubric_version": refs["rubric_version"], "case_count": len(assembled),
        "assembled_inputs": sum(len(result[0]) for _, result in assembled),
        "live_requests": 0, "generated_responses": 0, "handwritten_calibration_examples": len(calibration),
        "calibration_validation": "fixture_coverage_and_verbatim_evidence_only",
        "g5_metric_names": list(METRICS), "cases": report_cases,
        "sha256": {path.name: file_hash(path) for path in
                   (DATA / "cases.json", DATA / "reference.json", DATA / "calibration.json",
                    output / "model-inputs.jsonl")},
    }
    write_json(output / "validation.json", report)
    print(f"{report['status']}: {report['case_count']} cases, {report['assembled_inputs']} inputs, "
          f"{len(calibration)} handwritten calibration examples, 0 live requests")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
