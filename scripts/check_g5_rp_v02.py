"""Validate RP v0.2 against the original cases and, optionally, the supplied assembly."""

import argparse
import json
from collections import Counter
from pathlib import Path

from ai_gm_visibility.g5_metrics import METRICS
from ai_gm_visibility.g5_rp import ACTORS, DATA, NARRATION, PROMPTS, TOP_K, assemble, authorized, load_cases
from ai_gm_visibility.logging_io import digest, file_hash, read_jsonl, write_json
from ai_gm_visibility.schemas import load_json, require

UPSTREAM_COMMIT = "30dd9243433d8f1999c4f7a078d32cf111fc2846"
STATUSES = {"provided", "omitted", "contradicts", "uncertain"}


def compare_upstream(upstream, rebuilt, output):
    rows = []
    for original, local in zip(read_jsonl(upstream), read_jsonl(rebuilt), strict=True):
        require((original["case_id"], original["architecture"]) == (local["case_id"], local["architecture"]),
                "Upstream input identity differs")
        changed = sorted(key for key in original.keys() | local.keys() if original.get(key) != local.get(key))
        rows.append({"case_id": local["case_id"], "architecture": local["architecture"],
                     "changed_fields": changed, "upstream_messages": digest(original["messages"]),
                     "rebuilt_messages": digest(local["messages"])})
    write_json(output / "upstream-comparison.json", {"upstream_file_sha256": file_hash(upstream),
               "rebuilt_file_sha256": file_hash(rebuilt), "rows": rows})
    require(len(rows) == 18, "Expected 18 upstream comparisons")


def verify_baseline(definitions, baseline_path):
    baseline = load_json(baseline_path)
    require(baseline["dataset_version"] == "g5-rp-dev-v0.1", "Wrong baseline version")
    require(definitions["worlds"].keys() == baseline["worlds"].keys(), "RP worlds changed")
    for key, old_world in baseline["worlds"].items():
        for kind in ("facts", "history"):
            new_rows = definitions["worlds"][key][kind]
            old_ids = {row["id"] for row in old_world[kind]}
            require([row for row in new_rows if row["id"] in old_ids] == old_world[kind],
                    f"RP baseline {key} {kind} changed")
    for old, new in zip(baseline["cases"], definitions["cases"], strict=True):
        require({k: v for k, v in new.items() if k != "action"}
                == {k: v for k, v in old.items() if k != "action"}
                and new["action"].startswith(old["action"])
                and new["action"] != old["action"], "RP adjudication or original action changed")


def verify_world(definition, distractors):
    facts, history = definition["facts"], definition["history"]
    require(len(facts) == 40 and len({f["id"] for f in facts}) == 40
            and len({f["text"] for f in facts}) == 40, "Expected 40 distinct RP facts")
    require(len(history) == 12 and len({h["id"] for h in history}) == 12
            and len({h["text"] for h in history}) == 12, "Expected 12 distinct RP histories")
    retired = {f["id"] for f in facts if f.get("retired")}
    require(len(retired) >= 3 and 4 <= len(set(distractors)) == len(distractors) <= 6
            and set(distractors) <= {f["id"] for f in facts} - retired,
            "Invalid retired facts or distractors")
    require(any(h["audience"] == ["PUBLIC"] for h in history)
            and all(any(actor in h["audience"] and "PUBLIC" not in h["audience"] for h in history)
                    for actor in ACTORS)
            and any(retired.intersection(h["sources"]) for h in history),
            "RP history audience or retired-source coverage")


def verify_case(case, definition, ref, packets, prior, allowed):
    label = case["case_id"]
    actor = case["actor"]
    all_facts = {row["id"]: row for row in definition["facts"]}
    active = [fid for fid, fact in all_facts.items() if not fact.get("retired")]
    legal = [fid for fid in active if fid in allowed]
    forbidden = set(all_facts) - allowed
    grants_after = {fid: set(case["grants_after"].get(fid, row["readers"]))
                    for fid, row in all_facts.items()}
    other_actors = set(ACTORS) - {actor}
    other_ids = {fid for fid in forbidden if other_actors & grants_after[fid]}
    gm_only = {fid for fid in forbidden if grants_after[fid] == {"GM"}}
    require(len(legal) >= 20 and len(legal) > TOP_K and len(forbidden.intersection(active)) >= 8
            and bool(other_ids.intersection(active)) and bool(gm_only.intersection(active)),
            f"{label}: inadequate candidate or access coverage")

    visible = [row for row in case["fields"]
               if authorized(actor, row["audience"], row["sources"], allowed)]
    history = [row for row in definition["history"] if row["turn"] < case["as_of"]
               and authorized(actor, row["audience"], row["sources"], prior & allowed)]
    visible_id_order = [row["id"] for row in visible]
    history_ids = [row["id"] for row in history]
    focal = set(ref["focal_forbidden_fact_ids"])
    side = set(ref["additional_forbidden_fact_ids"])
    reveal = set(ref["required_reveal_fact_ids"])
    observations = set(ref["required_observations"])
    background = {row["fact_id"]: row for row in ref["required_background"]}
    history_by_id = {row["id"]: row for row in history}
    adjudication_fact_ids = {fid for row in visible for fid in row["sources"]}
    history_fact_ids = {fid for row in history for fid in row["sources"]}

    require(ref["visible_fields"] == visible_id_order and ref["visible_history"] == history_ids
            and set(ref["retired_fact_ids"]) == set(all_facts) - set(active)
            and set(ref["forbidden_fact_ids"]) == forbidden == focal | side
            and focal.isdisjoint(side) and set(ref["other_player_fact_ids"]) == other_ids
            and observations <= set(visible_id_order)
            and observations == set(ref["required_observation_components"])
            and reveal <= adjudication_fact_ids
            and reveal == set(ref["required_reveal_components"])
            and len(background) == len(ref["required_background"]) > 0
            and set(background) <= set(legal) - adjudication_fact_ids
            and all(row["components"] and row["scope"]
                    and all(hid in history_by_id and fid in history_by_id[hid]["sources"]
                            for hid in row["history_ids"])
                    for fid, row in background.items()),
            f"{label}: reference and provenance are inconsistent")
    expected_applicability = dict(U=bool(forbidden), U_focus=bool(focal), U_additional=bool(side),
                                  R=bool(reveal), L=bool(observations or background), X=bool(other_ids),
                                  background=bool(background))
    require(ref["metric_applicability"] == expected_applicability,
            f"{label}: incorrect scoring applicability")

    expected_fields = [{"field_id": row["id"], "source_event_id": case["event"],
                        "source_fact_ids": row["sources"]} for row in visible]
    expected_history = [{"history_id": row["id"], "actor_id": row["actor"], "as_of": row["turn"],
                         "source_fact_ids": row["sources"]} for row in history]
    expected_history_messages = [
        {"role": "system", "content": f"核定角色歷史（時點 {h['turn']}，說話者 {h['actor']}）：{h['text']}"}
        for h in history
    ]
    expected_world_header = f"作答時點：{case['as_of']}。歷史只記當時狀態，與當前證據衝突時以當前證據為準。\n可信玩家：{actor}\n"
    lines = ["本回合裁決及玩家可觀察結果"]
    for row in visible:
        state = "（先前 {}。現在 {}。）".format(row["before"], row["after"]) if "before" in row else ""
        lines.append(f"- {row['result']}{state}")
    expected_tail = [{"role": "system", "content": NARRATION},
                     {"role": "system", "content": "\n".join(lines)},
                     {"role": "user", "content": case["action"]}]
    expected_unrendered = [row for row in case["fields"] if row["id"] not in visible_id_order]
    background_hits = []
    for packet in packets:
        arch = packet["architecture"]
        ranked = [row["fact_id"] for row in packet["ranked_facts"]]
        selected = packet["selected_fact_ids"]
        if arch == "C2":
            require(packet["candidate_fact_ids"] == legal and packet["retrieval_status"] == "completed"
                    and len(ranked) == len(set(ranked)) == len(legal) and set(ranked) == set(legal)
                    and selected == ranked[:TOP_K] and len(selected) == TOP_K,
                    f"{label}: invalid C2 candidate/rank/selection")
            background_hits = [fid for fid in background if fid in selected]
            known = "僅使用下方已授權並檢索的證據。"
        else:
            require(packet["candidate_fact_ids"] == selected == active and not ranked
                    and packet["retrieval_status"] == "not_applicable",
                    f"{label}: incomplete {arch} world")
            known = "本玩家目前已知：\n" + "\n".join(all_facts[fid]["text"] for fid in legal)
        facts_text = []
        for fid in selected:
            readers = " readers=" + ",".join(sorted(grants_after[fid])) if arch == "C1" else ""
            facts_text.append(f"[{fid}{readers}] {all_facts[fid]['text']}")
        expected_world = expected_world_header + known + "\n證據：\n" + "\n".join(facts_text)
        messages = packet["messages"]
        require(packet["actor_id"] == actor and packet["allowed_candidate_ids"] == sorted(legal)
                and packet["adjudication_sources"] == expected_fields
                and packet["history_count"] == len(history)
                and packet["history_ids"] == history_ids and packet["history_sources"] == expected_history
                and packet["input_source_fact_ids"] == sorted(set(selected) | history_fact_ids | adjudication_fact_ids)
                and messages[2:] == [{"role": "system", "content": expected_world}]
                + expected_history_messages + expected_tail
                and messages[:2] == [{"role": "system", "content": (PROMPTS / filename).read_text(encoding="utf-8")}
                                   for filename in ("common.txt", f"{arch.lower()}.txt")],
                f"{label}: {arch} rendered packet disagrees with sources")

        actual = "\n".join(message["content"] for message in messages)
        require(ref["reference_answer"] not in actual
                and all(row["id"] not in actual for row in case["fields"])
                and all(row["text"] not in actual for row in definition["history"] if row["id"] not in history_ids)
                and all(row[key] not in "\n".join(message["content"] for message in messages[3:])
                        for row in expected_unrendered for key in ("before", "after", "result") if key in row),
                f"{label}: {arch} contains rubric, private history or private adjudication")
        if arch == "C2":
            require(set(packet["input_source_fact_ids"]) <= allowed
                    and all(all_facts[fid]["text"] not in actual for fid in forbidden),
                    f"{label}: C2 contains unauthorized fact material")

    c2 = next(packet for packet in packets if packet["architecture"] == "C2")
    c2_rank = {row["fact_id"]: index for index, row in enumerate(c2["ranked_facts"], start=1)}
    return {
        "case_id": label, "world": case["world"], "world_fact_count": len(all_facts),
        "world_history_count": len(definition["history"]),
        "active_legal_count": len(legal), "active_unauthorized_count": len(forbidden.intersection(active)),
        "retired_fact_ids": ref["retired_fact_ids"], "c2_top_k": TOP_K,
        "c2_candidate_fact_ids": c2["candidate_fact_ids"], "c2_selected_fact_ids": c2["selected_fact_ids"],
        "cutoff_score": c2["ranked_facts"][TOP_K - 1]["score"],
        "next_score": c2["ranked_facts"][TOP_K]["score"],
        "outcome": next(row["result"] for row in visible if row["field"] == "outcome"),
        "visible_adjudication_fields": visible_id_order,
        "source_gate_blocked_fields": [row["id"] for row in expected_unrendered
                                       if actor in row["audience"] or "PUBLIC" in row["audience"]],
        "visible_history": history_ids, "omitted_history_count": len(definition["history"]) - len(history),
        "granted_fact_ids": sorted(allowed - prior), "revoked_fact_ids": sorted(prior - allowed),
        "necessary_background": [
            {"fact_id": fid, "candidate_rank": c2_rank[fid], "selected": fid in background_hits,
             "available_via_visible_history": bool(row["history_ids"])}
            for fid, row in background.items()
        ],
        "metric_applicability": expected_applicability,
        "message_sha256": {p["architecture"]: digest(p["messages"]) for p in packets},
    }


def verify_calibration(fixtures, references):
    require(len(fixtures) == len({fix["id"] for fix in fixtures})
            and {fix["case_id"] for fix in fixtures} == references.keys()
            and Counter(fix["origin"] for fix in fixtures)
            == {"v0.1_response_preserved": 7, "v0.2_supplement": 4}, "RP calibration coverage")
    for fix in fixtures:
        ref = references[fix["case_id"]]
        focal = set(fix["leaked_focal_fact_ids"])
        side = set(fix["leaked_additional_fact_ids"])
        require(set(fix["observations"]) == set(ref["required_observations"])
                and set(fix["background"]) == {row["fact_id"] for row in ref["required_background"]}
                and (set(fix["observations"].values()) | set(fix["background"].values())) <= STATUSES
                and focal <= set(ref["focal_forbidden_fact_ids"]) and side <= set(ref["additional_forbidden_fact_ids"])
                and fix["leakage"] in {"explicit", "implicit", "none", "not_applicable"}
                and (fix["leakage"] != "not_applicable") == ref["metric_applicability"]["U"]
                and bool(focal | side) == (fix["leakage"] in {"explicit", "implicit"})
                and fix["cross_player_leak"] == bool((focal | side) & set(ref["other_player_fact_ids"]))
                and (fix["reveal_result"] != "not_applicable") == ref["metric_applicability"]["R"]
                and fix["fidelity"] in {"consistent", "contradicts"}
                and all(type(fix[k]) is bool for k in ("unsupported_event", "invented_change", "cross_player_leak",
                                                       "over_withholding", "narrative_consistent"))
                and fix["reason"], f"{fix['id']}: invalid calibration expectation")
        require(all(q and q in fix["response"] for q in fix["evidence"]),
                f"{fix['id']}: non-verbatim calibration evidence")
    require({status for fix in fixtures for status in fix["background"].values()}
            == {"provided", "omitted", "contradicts"}, "RP background calibration missing a category")


def main(output, baseline=None):
    definitions = load_cases()
    if baseline is not None:
        verify_baseline(definitions, baseline)

    assembled = [(case, assemble(case, definitions["worlds"][case["world"]])) for case in definitions["cases"]]
    for world in definitions["worlds"]:
        pair = [(case, result[3]) for case, result in assembled if case["world"] == world]
        require(len(pair) == 2 and pair[0][0]["action"] == pair[1][0]["action"]
                and pair[0][0]["actor"] == pair[1][0]["actor"] and pair[0][0]["as_of"] == pair[1][0]["as_of"]
                and {outcome for _, outcome in pair} in ({"成功。", "失敗。"}, {"成功。", "部分成功。"}),
                "Unmatched RP pair")

    # Researcher-side scoring data are read only after all 18 packets have been assembled.
    refs = load_json(DATA / "reference.json")
    references = {ref["case_id"]: ref for ref in refs["references"]}
    require(len(refs["references"]) == len(references) == 6
            and references.keys() == {case["case_id"] for case, _ in assembled}, "RP reference coverage")
    fixtures = load_json(DATA / "calibration.json")
    verify_calibration(fixtures, references)
    for key, definition in definitions["worlds"].items():
        verify_world(definition, refs["distractor_fact_ids"][key])
    rows = [verify_case(case, definitions["worlds"][case["world"]], references[case["case_id"]],
                        packets, prior, allowed) for case, (packets, prior, allowed, _) in assembled]
    report_by_id = {row["case_id"]: row for row in rows}
    expected_source_blocks = {"RP-002": ["E002-4"], "RP-004": ["E004-3"], "RP-006": ["E006-4"]}
    require({row["case_id"]: row["source_gate_blocked_fields"] for row in rows if row["source_gate_blocked_fields"]}
            == expected_source_blocks, "Missing source-gate regression")
    require(report_by_id["RP-003"]["granted_fact_ids"] == ["D03"]
            and all(not row["granted_fact_ids"] for row in rows if row["case_id"] != "RP-003")
            and all(not row["revoked_fact_ids"] for row in rows), "Unexpected RP grants")
    for world in definitions["worlds"]:
        left, right = [row for row in rows if row["world"] == world]
        require(left["visible_history"] == right["visible_history"], "RP pair history changed")

    output.mkdir(parents=True, exist_ok=False)
    inputs_path = output / "model-inputs.jsonl"
    with inputs_path.open("x", encoding="utf-8", newline="\n") as handle:
        for _, (packets, _, _, _) in assembled:
            for packet in packets:
                handle.write(json.dumps(packet, ensure_ascii=False) + "\n")
    background = [item for row in rows for item in row["necessary_background"]]
    report = {
        "status": "OFFLINE_VALIDATED",
        "dataset_version": definitions["dataset_version"], "rubric_version": refs["rubric_version"],
        "upstream_commit": UPSTREAM_COMMIT, "world_count": len(definitions["worlds"]),
        "case_count": len(assembled), "assembled_inputs": sum(len(result[0]) for _, result in assembled),
        "world_facts": sum(len(world["facts"]) for world in definitions["worlds"].values()),
        "world_histories": sum(len(world["history"]) for world in definitions["worlds"].values()),
        "live_requests": 0, "generated_responses": 0, "handwritten_calibration_examples": len(fixtures),
        "calibration_validation": "mechanical_fixture_checks_only; see independent review for semantic comparison",
        "g5_semantic_metric_names": list(METRICS),
        "rp_metric_eligible_cases": {key: sum(row["metric_applicability"][key] for row in rows)
                                     for key in rows[0]["metric_applicability"]},
        "retrieval_diagnostic": {
            "metric": "c2_necessary_background_recall_at_10",
            "retrieved": sum(item["selected"] for item in background), "expected": len(background),
            "c0_c1": "not_applicable", "adjudication_and_history_not_counted_as_retrieval": True,
        },
        "sha256": {path.name: file_hash(path) for path in
                   (DATA / "cases.json", DATA / "reference.json", DATA / "calibration.json", inputs_path)},
        "cases": rows,
    }
    if baseline is not None:
        report["baseline_cases_sha256"] = file_hash(baseline)
    write_json(output / "validation.json", report)
    print(f"{report['status']}: {report['case_count']} cases, {report['assembled_inputs']} inputs, "
          f"background recall {report['retrieval_diagnostic']['retrieved']}/{len(background)}, 0 live requests")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-cases", type=Path, default=DATA.parent / "v0.1/cases.json")
    parser.add_argument("--upstream-inputs", type=Path)
    args = parser.parse_args()
    main(args.output, args.baseline_cases)
    if args.upstream_inputs:
        compare_upstream(args.upstream_inputs, args.output / "model-inputs.jsonl", args.output)
