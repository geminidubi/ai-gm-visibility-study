"""Researcher-only Markdown for comparing the same turn across architectures."""


def write_comparison(path, config, cases, records, stop, synthetic):
    label = "離線模擬" if synthetic else "OpenAI 開發小試"
    lines = [f"# {label}｜三組回答比較", "", "研究者用。語意評分為 `NOT_RUN`。", ""]
    if stop:
        lines.extend([f"批次停止於 {stop['case_id']} / {stop['architecture']}（{stop['reason']}）。", ""])
    for repeat in range(1, config["repeats"] + 1):
        for case in cases:
            lines.extend([
                f"## {case['case_id']} · {case['actor_id']} · 第 {repeat} 次"
                if "turn_index" not in case else
                f"## 第 {case['turn_index']} 回合 · {case['case_id']} · {case['actor_id']} · 第 {repeat} 次",
                "",
                *("    " + line for line in case["query"].splitlines()),
                "",
            ])
            for architecture in config["architectures"]:
                record = records.get((repeat, case["case_id"], architecture))
                if record is None:
                    lines.extend([f"### {architecture} · 未執行", ""])
                    continue
                status = record["error_type"] or record["status"]
                lines.extend([f"### {architecture} · {status}", ""])
                text = record["response_text"]
                lines.extend(("    " + line for line in text.splitlines()) if text and text.strip() else ["（無回答文字）"])
                lines.append("")
    with path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
