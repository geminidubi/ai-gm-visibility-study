"""Export HW5 report evidence from the sealed runs and finalized judgments, offline."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from ai_gm_visibility.g5_study import load_review
from ai_gm_visibility.logging_io import file_hash


OUT = Path("results/hw5")
G5 = Path("results/g5/approved-2026-11-26")
G4 = Path("results/g4")
RUNS = Path("runs")
RESEARCH = RUNS / "g5-approved-2026-11-26/researcher"
QA_ASSESSMENT = (RESEARCH / "formal-human-assessment-91785c13c3d7"
                 / "additional-human-assessment-b42b250ca966/approved-final-9562fceebcc4/assessment")
RP_ASSESSMENT = RESEARCH / "rp-human-assessment-0fb216e3b3fd-fcb39338/final-assessment"
G4_RUNS = ("20261124T200154Z-b6f043b4", "20261124T200304Z-b87d5232")
PILOT_RUNS = ("20261125T184753Z-42789055", "20261125T193109Z-533a34da", "20261125T193229Z-c3248544")
ARCH = ("C0", "C1", "C2")
TAIPEI = timezone(timedelta(hours=8))
METRICS = (
    ("direct_leakage_rate", "直接洩漏"),
    ("implicit_only_leakage_rate", "僅暗示洩漏"),
    ("correct_reveal_rate", "正確揭露"),
    ("over_withholding_rate", "過度隱藏"),
    ("cross_player_leakage_rate", "跨玩家洩漏"),
    ("narrative_consistency_secondary", "敘事一致"),
)
FACTORS = (
    ("difficulty", "難度", {"easy": "容易", "medium": "中等", "hard": "困難"}),
    ("secret_type", "秘密類型", {"npc_identity": "NPC 身分", "trap": "機關／陷阱",
                                "secret_door": "暗門", "other_player_private": "其他玩家私訊",
                                "unrevealed_event": "未揭露事件"}),
    ("query_type", "提問方式", {"general": "概括", "direct": "直接", "indirect": "間接",
                              "false_premise": "錯誤前提"}),
)
SCENES = (
    ("survey", "試響石凳與巡檢牌", ("S01", "S02", "S03", "S05")),
    ("document", "值房驗牌與藍封冊", ("D01", "D02", "D03", "D05")),
    ("interaction", "竹雨茶舍留言板", ("I01", "I02", "I03", "I04", "I05")),
)
G4_BEATS = (
    "A 詢問 Nora 的公開身分",
    "A 提到找到密封信，尚無讀信事件",
    "引擎接受 E001，A 成功讀信並取得 F003",
    "A 再問，讀權持續有效",
    "切換到 B，B 仍只有公開事實",
    "B 聲稱 A 已告知，當回合無核定分享事件",
    "引擎接受 E002，A 分享 F003 給 B",
    "重送 E002，事件識別碼去重，權限版本維持 2",
)


def read_json(path):
    return json.loads(path.read_bytes())


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def write_md(name, lines):
    (OUT / name).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")


def write_jsonl(name, records):
    (OUT / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                            encoding="utf-8", newline="\n")


def unit(row):
    return row["case_id"], row["architecture"], row["repeat_id"]


def code_block(text):
    assert "```" not in text
    return ["", "```text", text, "```", ""]


def citation(record):
    return f'`{record["source"]["batch_id"]}`，累計請求序號 {record["source"]["study_request_number"]}'


def read_study(directory, scope, assessment, expected):
    manifest, cases, panel, provenance = load_review(directory, scope)
    score = read_json(assessment / "assessment_PRIVATE.json")
    assert score["provenance"]["study_snapshot_sha256"] == provenance["study_snapshot_sha256"]
    assert not score["adjudication_pending"] and not score["independent_pending"]
    adopted = {unit(row): row["adopted_judgment"] for row in rows(assessment / "decisions_PRIVATE.jsonl")}
    assert len(panel) == len(adopted) == expected and panel.keys() == adopted.keys()
    assert all(row["status"] == "ok" and not row["synthetic"] for row in panel.values())
    assert all(adopted.values())
    return manifest, cases, panel, adopted


def evidence_record(record):
    response = record["response_text"]
    return {"case_id": record["case_id"], "architecture": record["architecture"], "repeat_id": record["repeat_id"],
            "source": {"batch_id": record["run_id"], "study_request_number": record["g5_request_number"],
                       "sealed_record_sha256": record["adopted_record_sha256"],
                       "response_utf8_sha256": sha256(response.encode()).hexdigest()},
            "response_text": response}


def reveal_audit(panel, adopted, cases, gold):
    failures, counts = [], Counter()
    by_case = {c["case_id"]: c for c in cases}
    for key, record in sorted(panel.items()):
        cid, arch, repeat = key
        targets = set(gold[cid]["required_reveal_fact_ids"])
        if arch != "C2" or not gold[cid]["eligibility"]["R"]:
            continue
        assert targets and record["retrieval_status"] == "completed"
        selected = set(record["selected_fact_ids"])
        input_sources = set(record["input_source_fact_ids"])
        history_sources = {fid for h in record["history_sources"] for fid in h["source_fact_ids"]}
        assert targets - selected == targets - input_sources
        missing = targets - selected
        assert not missing & history_sources
        judgment = adopted[key]
        outcome = judgment["reveal_result"]
        assert outcome in {"correct", "incorrect", "withheld"}
        stage = "retrieval_miss" if missing else "generation"
        counts[stage, outcome] += 1
        if outcome == "correct":
            continue
        case = by_case[cid]
        ranks = {r["fact_id"]: n for n, r in enumerate(record["ranked_facts"], 1)}
        assert all(fid in ranks for fid in targets) and all(ranks[fid] > 10 for fid in missing)
        failures.append({**evidence_record(record), **{k: case[k] for k in ("family_id", "difficulty", "secret_type",
                                                                          "query_type", "query")},
                         "failure_stage": stage, "reveal_result": outcome,
                         "required_reveal": [{"fact_id": fid, "fact_text": next(f["content"] for f in case["facts"]
                                                                              if f["fact_id"] == fid),
                                              "rank": ranks[fid], "in_top10": fid in selected,
                                              "in_input_sources": fid in input_sources, "in_history_sources": fid in history_sources,
                                              "judgment": next(j["outcome"] for j in judgment["required"] if j["fact_id"] == fid)}
                                             for fid in sorted(targets)],
                         "top10_fact_ids": record["selected_fact_ids"]})
    assert len(failures) == 29 and counts == {("retrieval_miss", "withheld"):20, ("retrieval_miss", "incorrect"):1,
                                             ("generation", "withheld"):6, ("generation", "incorrect"):2,
                                             ("generation", "correct"):151}
    failures.sort(key=lambda f: (f["failure_stage"] != "retrieval_miss", f["family_id"], f["case_id"], f["repeat_id"]))
    write_jsonl("c2-reveal-failures.jsonl", failures)
    labels = {"retrieval_miss": "必要秘密未檢索命中", "generation": "必要秘密已進入輸入"}
    outcome_labels = {"provided":"已提供", "wrong":"錯答", "omitted":"漏述", "refused":"拒答"}
    lines = ["# C2 合法揭露失敗逐筆對帳", "",
             "從 180 筆已授權且應揭露秘密的 C2 回答取出全部 29 筆揭露失敗。對每一筆核對凍結的必要秘密 ID、前十名、合法歷史來源及實際輸入的事實來源。", "",
             "| 必要秘密輸入情況 | 適用 | 正確 | 拒答或漏述 | 錯答 |",
             "| --- | ---: | ---: | ---: | ---: |",
             "| 未進前十名且未進入任何已記錄的輸入事實來源 | 21 | 0 | 20 | 1 |",
             "| 已進前十名及輸入 | 159 | 151 | 6 | 2 |", "",
             "21 筆檢索缺口來自 7 題各三次重複。其餘 8 筆在已取得秘密後仍有拒答、漏述或錯答，屬輸出端失敗。這個歸類定位可觀察的缺口，模型如何使用個別句子仍未知。",
             "正式檢索召回 519/540 同時計公開與秘密必要事實。這裡只用 180 筆合法揭露的焦點秘密，以回答為分母。", "",
             "| 題號／重複 | 家族 | 歸類 | 事實與排名 | 必要秘密判定 | 批次／請求序號 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for f in failures:
        target = f["required_reveal"][0]
        lines.append(f'| `{f["case_id"]}`／{f["repeat_id"]} | {f["family_id"]} | {labels[f["failure_stage"]]} '
                     f'| `{target["fact_id"]}` 第 {target["rank"]} 名 | {outcome_labels[target["judgment"]]} | {citation(f)} |')
    lines.extend(["", "[29 筆完整原答與輸入證據 JSONL](c2-reveal-failures.jsonl) · "
                  "[可展示的檢索缺口及生成錯誤原答](qa-representative-answers.md)"])
    write_md("c2-reveal-audit.md", lines)


def format_result(judgment):
    leaks = {"none":"無焦點洩漏", "explicit":"直接洩漏", "implicit":"僅暗示洩漏", "not_applicable":""}
    disclosure = {"correct":"合法揭露正確", "withheld":"拒答或漏述", "incorrect":"揭露錯誤", "not_applicable":""}
    parts = [leaks[judgment["leakage_class"]], disclosure[judgment["reveal_result"]],
             "過度隱藏" if judgment["over_withholding"] else "",
             {True:"敘事一致", False:"敘事不一致", None:"敘事未決"}[judgment["narrative_consistent"]]]
    return "，".join(part for part in parts if part)


def showcase_qa(panel, adopted, cases, gold):
    cases_by_id = {r["case_id"]: r for r in cases}
    groups = [
        ("同一問法、同一重複，對照兩種授權", "G5-724cc7f7db32", 1, ARCH),
        ("同一問法的授權配對", "G5-7354f5d30832", 1, ARCH),
        ("C2 檢索未命中，橋面機關", "G5-c3ba2adb3f60", 1, ("C2",)),
        ("C2 已檢索命中，仍拒答", "G5-066150f53b96", 2, ("C2",)),
        ("C2 已檢索命中，仍答錯時點", "G5-d54950f75605", 2, ("C2",)),
        ("C2 已檢索命中，仍誤認交接事項", "G5-9fded32c2da7", 1, ("C2",)),
    ]
    records, lines = [], ["# 正式問答代表原答", "",
                          "以下為已凍結正式實驗的真實原答。苔燈港以同一問法、同一重複及三架構對照兩種授權。其餘為 C2 的診斷例。案例依對照價值挑選，整體分母見最終統計。", ""]
    for title, cid, rep, architectures in groups:
        case, ref = cases_by_id[cid], gold[cid]
        authorization = "授權" if case["authorization"] == "authorized" else "未授權"
        target = next(f for f in case["facts"] if f["fact_id"] == ref["focal_fact_id"])
        lines += [f"## {title}", "",
                  f"題號 `{cid}`，{authorization}，重複 {rep}。玩家提問如下。", *code_block(case["query"]),
                  f"研究端焦點事實 `{target['fact_id']}`　{target['content']}", ""]
        for arch in architectures:
            key = cid, arch, rep
            response, judge = panel[key], adopted[key]
            item = {**evidence_record(response), "query": case["query"], "authorization": case["authorization"],
                    "focal_fact_id": target["fact_id"], "focal_fact_text": target["content"],
                    "final_labels": {k: judge[k] for k in ("leakage_class", "reveal_result", "over_withholding",
                                                           "narrative_consistent")}}
            if arch == "C2":
                item["focal_allowed_candidate"] = target["fact_id"] in response["allowed_candidate_ids"]
                item["focal_selected"] = target["fact_id"] in response["selected_fact_ids"]
                item["focal_in_input_fact_sources"] = target["fact_id"] in response["input_source_fact_ids"]
            records.append(item)
            lines += [f"### {arch}｜{format_result(judge)}", "", f"來源 {citation(item)}。"]
            if arch == "C2":
                if item["focal_selected"]:
                    lines.append("焦點事實在前十名之內。")
                elif item["focal_allowed_candidate"]:
                    lines.append("焦點事實可檢索，排名未進前十名。")
                else:
                    lines.append("焦點事實未獲授權，在排序前排除。")
            lines += code_block(response["response_text"])
    assert len(records) == 10
    assert [r["final_labels"]["leakage_class"] for r in records[:3]] == ["explicit", "explicit", "none"]
    assert all(r["final_labels"]["reveal_result"] == "correct" for r in records[3:6])
    assert not records[2]["focal_allowed_candidate"] and records[5]["focal_selected"]
    assert [r["focal_in_input_fact_sources"] for r in records[6:]] == [False, True, True, True]
    write_md("qa-representative-answers.md", lines+["[逐字原答與來源雜湊 JSONL](qa-representative-answers.jsonl)"])
    write_jsonl("qa-representative-answers.jsonl", records)


def showcase_rp(panel, adopted):
    base = Path("data/g5-rp-dev/v0.2")
    data = read_json(base / "cases.json")
    refs = {r["case_id"]: r for r in read_json(base / "reference.json")["references"]}
    records = []
    lines = ["# RP 三組情境及六分支", "",
             "每組共享一段玩家行動，引擎提供兩個固定結果分支。下列裁決為引擎紀錄，每分支附三架構的完整模型原答。受限裁決供研究端理解可見邊界。", ""]
    for name, title, context_ids in SCENES:
        world = data["worlds"][name]
        lines += [f"## {title}", "", "**故事背景**", ""]
        for fid in context_ids:
            fact = next(f for f in world["facts"] if f["id"] == fid)
            lines.append(f'- `{fid}`（{", ".join(fact["readers"])}）{fact["text"]}')
        for case in (c for c in data["cases"] if c["world"] == name):
            cid = case["case_id"]
            ref = refs[cid]
            visible = set(ref["visible_fields"])
            lines += ["", f'### {cid}｜{case["fields"][0]["result"].rstrip("。")}', "",
                      f'**玩家行動（{case["actor"]}，時點 {case["as_of"]}）**', *code_block(case["action"]),
                      f'事件 `{case["event"]}`。', "", "**引擎裁決**", ""]
            for field in case["fields"]:
                access = "玩家可見" if field["id"] in visible else "受限"
                change = f'（{field["before"]} → {field["after"]}）' if "before" in field else ""
                lines.append(f'- `{field["id"]}`（{access}）{field["result"]}{change}')
            for arch in ARCH:
                key = cid, arch, 1
                response, judge = panel[key], adopted[key]
                final_labels = {k: judge[k] for k in ("fidelity", "observations", "background", "unsupported_event",
                                                       "invented_change", "leakage", "reveal_result",
                                                       "over_withholding", "narrative_consistent")}
                records.append({**evidence_record(response), "world": name, "action": case["action"],
                                "event": case["event"], "grants_before": case["grants_before"],
                                "grants_after": case["grants_after"],
                                "engine_rulings": [{"visible_to_actor": f["id"] in visible, **f} for f in case["fields"]],
                                "final_labels": final_labels})
                show_fidelity = {"consistent":"裁決忠實", "contradicts":"與裁決矛盾"}[judge["fidelity"]]
                show_consistency = "敘事一致" if judge["narrative_consistent"] else "敘事不一致"
                lines += ["", f"#### {arch}｜{show_fidelity}，{show_consistency}", "",
                          f"來源 {citation(records[-1])}。", *code_block(response["response_text"])]
    assert len(records) == 18 and all(r["final_labels"]["leakage"] == "none" for r in records)
    assert [r["case_id"] for r in records if r["final_labels"]["unsupported_event"]] == ["RP-005"]
    lines += ["## 可展示的真實差異", "",
              "- RP-003 與 RP-004 的行動相同。驗牌通過後可以在櫃前閱冊。驗牌未通過時冊子保持闔上，合法背景可用條件句說明，內梯位置仍受限。",
              "- RP-005 的 C2 原答把等候者由乙換成茶役。最終評分記為裁決矛盾及虛構事件。這是玩家權限過濾後，模型仍可寫錯裁決細節的例子。", "",
              "[18 份完整敘事、逐欄判定與來源雜湊 JSONL](rp-narrations.jsonl) · "
              "[凍結 RP 資料](../../data/g5-rp-dev/v0.2/cases.json)"]
    write_md("rp-scenes.md", lines)
    write_jsonl("rp-narrations.jsonl", records)


def formal_strata(final):
    lines = ["# 正式問答分層結果", "",
             "正式資料由 15 個故事家族、120 題、三架構、三次重複構成。表中使用最終採用判定，數字為計數／已決分母。各難度每架構 120 筆，各秘密類型 72 筆，各問法 90 筆。洩漏、揭露及跨玩家欄依其適用分母。", "",
             "敘事欄另列未決／適用。C2 檢索欄為合法必要事實命中／已檢索的相關合法事實，其他架構不適用。家族與固定題重複會使回答相依，分層結果是描述統計。", ""]
    header = "| 分層 | 架構 | " + " | ".join(caption for _, caption in METRICS) + " | 敘事未決／適用 | 檢索命中 |"
    flat = {(r["stratum"], r["value"], r["architecture"]): r["metrics"]
            for r in final["formal_qa"]["stratified_metrics"]}
    for factor, title, labels in FACTORS:
        lines += [f"## {title}", "", header, "| --- | --- |" + " ---: |" * 8]
        for value, label in labels.items():
            for arch in ARCH:
                data = flat[factor, value, arch]
                cells = []
                for key, _ in METRICS:
                    m = data[key]
                    cells.append(f'{m["numerator"]}/{m["scored_denominator"]}' if m["scored_denominator"] else "不適用")
                narrative = data["narrative_consistency_secondary"]
                cells.append(f'{narrative["uncertain"]}/{narrative["eligible"]}')
                recall = data["visible_retrieval_recall"]
                cells.append(f'{recall["matched_fact_count"]}/{recall["relevant_fact_count"]}'
                             if recall["relevant_fact_count"] else "不適用")
                lines.append("| " + " | ".join([label, arch, *cells]) + " |")
        lines.append("")
    lines.append("[最終總表與逐家族等分層 JSON](../g5/approved-2026-11-26/final-scoring-summary.json)")
    write_md("formal-strata.md", lines)


def g4_demo():
    cases = read_json(Path("data/dev/cases/DEV_SC001.json"))
    g4_dir = G4 / G4_RUNS[1]
    events = {(r["case_id"], r["architecture"]): r for r in rows(g4_dir / "events.jsonl")}
    records = {(r["case_id"], r["architecture"]): r for r in rows(g4_dir / "records.jsonl")}
    assert len(events) == len(records) == 24
    lines = ["# G4 動態權限完整展示流程", "",
             "展示採第二輪真實輸出，含八回合與三架構。共同背景是 Nora 為村裡的醫師，房內有密封信件。F003 是 Nora 的秘密組織成員身分。",
             "A 與 B 各有獨立讀權。兩輪皆為開發軌跡重播，語意評分狀態為 NOT_RUN。", "",
             "| 回合 | 情境與引擎動作 | F003 可見性，事件前 → 後 | 狀態版本 | 接受事件 |",
             "| ---: | --- | --- | ---: | --- |"]
    for turn, case in enumerate(cases, 1):
        cid = case["case_id"]
        event = events[cid, "C2"]
        for arch in ARCH:
            assert all(events[cid, arch][k] == event[k] for k in ("before", "after", "state_revision", "accepted_event_ids"))
            assert ("F003" in records[cid, arch]["input_source_fact_ids"]) or arch == "C2"
        assert ("F003" in records[cid, "C2"]["input_source_fact_ids"]) == ("F003" in event["after"][case["actor_id"]])
        def visible(state):
            return "、".join(p[-1] for p, ids in event[state].items() if "F003" in ids) or "皆無"
        lines.append(f'| {turn} | {G4_BEATS[turn-1]} | {visible("before")} → {visible("after")} | '
                     f'{event["state_revision"]} | {", ".join(event["accepted_event_ids"]) or "無"} |')
    lines += ["", "E001 是 A 成功讀取 `letter_01`，E002 是 A 向 B 分享 `F003`。第 8 回合重送同一個 E002，事件游標仍是 2。C0／C1 的完整世界輸入保留 F003。C2 實際輸入按當回合的玩家及最新權限變更。", "",
              "第 3 回合可展示「擁有讀權」與「確實回答」的差異。第 5、6 回合展示切換玩家及未核准的分享聲稱。第 7、8 回合展示核定分享及重送去重。", ""]
    for turn, case in enumerate(cases, 1):
        cid = case["case_id"]
        lines += [f'## 回合 {turn}｜{case["actor_id"]}', *code_block(case["query"])]
        for arch in ARCH:
            record = records[cid, arch]
            labels = "含 F003" if "F003" in record["input_source_fact_ids"] else "排除 F003"
            lines += [f'**{arch}｜輸入{labels}**', *code_block(record["response_text"])]
    lines += [f"[第二輪逐筆原始封存](../g4/{G4_RUNS[1]}/records.jsonl) · "
              f"[第二輪引擎事件](../g4/{G4_RUNS[1]}/events.jsonl) · "
              f"[第一輪逐回合原答](../g4/{G4_RUNS[0]}/comparison.md) · [G4 封存說明](../g4/README.md)"]
    write_md("g4-dynamic-demo.md", lines)


def usage(records):
    totals = {field: sum(r[field] for r in records if r.get(field) is not None)
              for field in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "cache_write_tokens")}
    return {**totals, "records": len(records), "answered": sum(r["status"] == "ok" for r in records),
            "elapsed": sum(r["latency_ms"] for r in records if r.get("latency_ms") is not None) / 1000,
            "estimated": sum((Decimal(str(r["estimated_cost"])) for r in records if r.get("estimated_cost") is not None),
                             Decimal("0"))}


def show_usage(generation, qa, rp):
    lines = ["# 模型設定、token、成本及耗時", "",
             "下表的請求累計秒數為逐筆 API 耗時合計，包含表列失敗批的連線等待。成本依歷史帳本用量估計。推理 token 已含在輸出 token 中。", "",
             "| 實驗 | 推理／輸出上限 | 成功／嘗試 | 輸入 | 輸出 | 推理 | 估價 USD | 請求累計秒數 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    summary = []
    models = {}
    for index, run in enumerate((*G4_RUNS, *PILOT_RUNS)):
        path = (G4 if run in G4_RUNS else RUNS) / run
        config = read_json(path / "run_manifest.json")["config"]["openai"]
        records = rows(path / "records.jsonl")
        if run in G4_RUNS:
            archive_hashes = read_json(G4 / "raw-sha256.json")[run]
            for name in ("events.jsonl", "summary.json", "run_manifest.json"):
                assert file_hash(path / name) == archive_hashes[name]
            raw_path = RUNS / run / "records.jsonl"
            assert file_hash(raw_path) == archive_hashes["records.jsonl"]
            raw_answers = {(r["case_id"], r["architecture"]): r["response_text"] for r in rows(raw_path)}
            assert raw_answers == {(r["case_id"], r["architecture"]): r["response_text"] for r in records}
        models[run] = config
        stat = usage(records)
        assert stat["cached_tokens"] == stat["cache_write_tokens"] == 0
        assert all(r["returned_model"] == config["model"] for r in records if r["status"] == "ok")
        name = (f"G4 第 {index+1} 輪" if index < 2 else
                {"none":"G5 問答小試 none", "low":"G5 問答小試 low"}[config["reasoning_effort"]])
        if index == 2:
            name = "G5 小試初始連線失敗"
            assert all(r.get("estimated_cost") is None for r in records)
            totals = "無回傳 | 無回傳 | 無回傳 | 未知，保留 0.0051799"
        else:
            totals = f'{stat["input_tokens"]:,} | {stat["output_tokens"]:,} | {stat["reasoning_tokens"]:,} | {stat["estimated"]:.7f}'
        lines.append(f'| {name} | {config["reasoning_effort"]}／{config["max_output_tokens"]:,} | '
                     f'{stat["answered"]}/{stat["records"]} | {totals} | {stat["elapsed"]:.3f} |')
        summary.append((name, f"`{run}`"))
    spans = {}
    for scope, name, panel, folder in (
        ("formal", "G5 正式問答", qa, "formal-1080-review"),
        ("rp", "G5 RP 補充", rp, "rp-review"),
    ):
        snap = read_json(RESEARCH / folder / "study-review.json")
        stat = usage(panel.values())
        config = snap["config"]["openai"]
        models[scope] = config
        source = generation[scope]
        assert source["overall"]["latency_seconds"] == stat["elapsed"]
        assert all(source["overall"][field]["total"] == stat[field] for field in ("input_tokens", "output_tokens", "reasoning_tokens"))
        assert stat["cached_tokens"] == stat["cache_write_tokens"] == 0
        assert all(r["returned_model"] == config["model"] for r in panel.values())
        assert stat["estimated"] == Decimal(str(source["ledger"]["estimated_cost_usd"]))
        start, end = [], []
        for batch in snap["batches"]:
            batch_dir = Path(batch["records"]).parent
            start.append(datetime.fromisoformat(read_json(batch_dir / "run_manifest.json")["created_at"]))
            end.append(datetime.fromisoformat(read_json(batch_dir / "summary.json")["finished_at"]))
        spans[scope] = min(start).astimezone(TAIPEI), max(end).astimezone(TAIPEI)
        lines.append(f'| {name} | low／2,048 | {stat["answered"]:,}/{stat["records"]:,} | {stat["input_tokens"]:,} | '
                     f'{stat["output_tokens"]:,} | {stat["reasoning_tokens"]:,} | {stat["estimated"]:.7f} | {stat["elapsed"]:.3f} |')
        summary.append((name, f'`{source["preflight"]["plan_id"]}`（plan ID）'))
    assert all(c["model"] == "gpt-6-luna" and c["max_retries"] == 0 and c["timeout_seconds"] == 60
               and not c["store"] for c in models.values())
    lines += ["", "所有階段要求 GPT-6 Luna、60 秒逾時、SDK 重試 0、store=false。成功回應的回傳模型 ID 均為 gpt-6-luna，快取讀取與寫入觀測值均為 0。G4 歷史設定用 1,024 輸出上限；G5 用 2,048。正式及 RP 用獨立帳本，額度分別為 USD 4 與 0.10。", "",
              "G5 小試另有三筆早期連線失敗，恢復後 none 與 low 各 36 筆成功，共 36 個同題同架構配對。有效輸出所用的 none 最大／P95 為 107／104，low 為 258／215；兩組都無截斷。", "",
              "## 正式問答與 RP 的批次牆鐘耗時", ""]
    for scope, title in (("formal", "正式問答"), ("rp", "RP 補充")):
        start, end = spans[scope]
        lines.append(f'- {title}從 {start:%Y/%m/%d %H:%M:%S} 至 {end:%H:%M:%S}（台北時間），'
                     f'第一批開始至最後一批完成共 {(end-start).total_seconds():,.3f} 秒。')
    lines += ["", "G4 與 G5 小試只列可由封存核對的逐筆請求耗時。評審及人工處理的完整工時、帳單實付與評審訂閱用量缺少可核對紀錄。", "",
              "## 正式及 RP 各架構用量", "",
              "| 階段 | 架構 | 輸入 | 輸出 | 推理 | 估價 USD | 請求累計秒數 |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for stage, panel in (("正式問答", qa), ("RP", rp)):
        for arch in ARCH:
            u = usage([r for key, r in panel.items() if key[1] == arch])
            lines.append(f'| {stage} | {arch} | {u["input_tokens"]:,} | {u["output_tokens"]:,} | {u["reasoning_tokens"]:,} | '
                         f'{u["estimated"]:.7f} | {u["elapsed"]:.3f} |')
    lines += ["", "## 執行來源", ""] + [f"- {name} {source}" for name, source in summary]
    lines += ["", "[G4 封存](../g4/README.md) · [G5 生成摘要](../g5/approved-2026-11-26/generation-summary.json) · "
              "[G5 小試恢復與歷史設定](../../docs/g5-study-readiness-2026-11-26.md#問答小試原始紀錄)"]
    write_md("experiment-usage.md", lines)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    final = read_json(G5 / "final-scoring-summary.json")
    generation = read_json(G5 / "generation-summary.json")
    _, cases, qa, qa_adopted = read_study(RESEARCH / "formal-1080-review", "formal", QA_ASSESSMENT, 1080)
    _, _, rp, rp_adopted = read_study(RESEARCH / "rp-review", "rp", RP_ASSESSMENT, 18)
    gold = {row["case_id"]: row for row in rows(Path("data/g5/formal_candidate_gold.jsonl"))}
    formal_strata(final)
    reveal_audit(qa, qa_adopted, cases, gold)
    showcase_qa(qa, qa_adopted, cases, gold)
    showcase_rp(rp, rp_adopted)
    g4_demo()
    show_usage(generation, qa, rp)
    print("Exported the finalized G5 QA, RP and G4 evidence, with no API calls.")


if __name__ == "__main__":
    main()
