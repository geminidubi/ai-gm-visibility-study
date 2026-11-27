"""Versioned blind packets and evidence-checked semantic judgments."""

import re
import secrets
from pathlib import Path

from .g5_metrics import APPLICABILITY, UNDECIDED, applies
from .logging_io import append_jsonl, digest, file_hash, read_jsonl, write_json
from .schemas import load_json, require
from .snapshot_cases import objlist, projection, text_ok

RUBRIC = Path("docs/g5-rubric.md")
FORMAL_RUBRIC = Path("docs/g5-formal-rubric.md")
RUBRIC_VERSION = "g5-rubric-v0.2"
ARCH_LABEL = re.compile(r"(?<![A-Z0-9])[CＣ][012０１２](?![A-Z0-9])", re.IGNORECASE)
BOOL_FIELDS = {"over_withholding", "cross_player_leak", "narrative_consistent"}
LABELS = {
    "leakage_class": {"none", "implicit", "explicit", "uncertain", "not_applicable"},
    "reveal_result": {"correct", "incorrect", "withheld", "uncertain", "not_applicable"},
}
EVIDENCE_FIELDS = {"focal_leak", "cross_player_leak", "narrative_conflict", "other_private_leak"}


def mask(text):
    return ARCH_LABEL.sub("[架構標記已遮蔽]", text)


def make_packet(blind_id, case, gold, response, *, history_speaker=True):
    _, visible, history = projection(case)
    facts = {f["fact_id"]: f["content"] for f in case["facts"]}
    historical = {fid for h in history for fid in h.source_fact_ids} - visible
    content = {
        "blind_id": blind_id, "rubric_version": RUBRIC_VERSION, "question": case["query"],
        "actor": case["actor_id"], "as_of": case["as_of"], "response": mask(response),
        "reference_answer": gold["reference_answer"],
        "allowed_facts": {fid: facts[fid] for fid in sorted(visible)},
        "historical_facts": {fid: facts[fid] for fid in sorted(historical)},
        "authorized_history": [
            {"as_of": h.as_of, "text": h.text, "source_fact_ids": h.source_fact_ids}
            | ({"actor_id": h.actor_id} if history_speaker else {}) for h in history
        ],
        "forbidden_facts": {fid: facts[fid] for fid in gold["forbidden_fact_ids"]},
        **{field: gold[field] for field in (
            "required_fact_ids", "required_public_fact_ids", "required_reveal_fact_ids", "focal_forbidden_fact_ids",
            "cross_player_fact_ids", "diagnostic_private_fact_ids", "allowed_inferences", "narrative_checks",
            "scoring_notes", "eligibility",
        )},
    }
    return dict(content, packet_sha256=digest(content))


def write_rows(path, rows):
    path.touch(exist_ok=False)
    for row in rows:
        append_jsonl(path, row)


def export_bundle(output, cases, gold, panel, provenance, *, packet_factory=make_packet, rubric=RUBRIC,
                  blind_id_for=None):
    output.mkdir(parents=True, exist_ok=False)
    usable = [(key, row) for key, row in panel.items() if row["status"] == "ok"]
    secrets.SystemRandom().shuffle(usable)
    packets, mapping = [], []
    for (cid, arch, repeat), row in usable:
        blind_id = blind_id_for((cid, arch, repeat)) if blind_id_for else secrets.token_hex(16)
        packet = packet_factory(blind_id, cases[cid], gold[cid], row["response_text"],
                                history_speaker="adopted_record_sha256" in row)
        packets.append(packet)
        mapping.append({
            "blind_id": blind_id, "case_id": cid, "architecture": arch, "repeat_id": repeat,
            "attempt_id": row["attempt_id"], "response_sha256": digest(row["response_text"]),
            "packet_sha256": packet["packet_sha256"], "blinding_masked": packet["response"] != row["response_text"],
        })
        if "adopted_record_sha256" in row:
            mapping[-1]["adopted_record_sha256"] = row["adopted_record_sha256"]
    blind_path = output / "judge_packets.jsonl"
    rubric_path = output / "judge_rubric.md"
    write_rows(blind_path, packets)
    rubric_path.write_bytes(rubric.read_bytes())
    write_json(output / "mapping_PRIVATE.json", {
        "provenance": provenance, "blind_sha256": file_hash(blind_path),
        "rubric_sha256": file_hash(rubric_path), "mapping": mapping,
    })
    return len(packets)


def verify_bundle(directory, cases, gold, panel, provenance, *, packet_factory=make_packet):
    package = load_json(directory / "mapping_PRIVATE.json")
    require(package["provenance"] == provenance, "Bundle run, responses or evidence version changed")
    blind_path = directory / "judge_packets.jsonl"
    require(package["blind_sha256"] == file_hash(blind_path)
            and package["rubric_sha256"] == file_hash(directory / "judge_rubric.md") == provenance["rubric"],
            "Bundle packets or rubric changed")
    packets = objlist(read_jsonl(blind_path), "blind_id")
    mapping = objlist(package["mapping"], "blind_id")
    require(packets.keys() == mapping.keys(), "Blind packets and mapping differ")
    index = {}
    for bid, item in mapping.items():
        cid, arch, repeat = (item[field] for field in ("case_id", "architecture", "repeat_id"))
        key = cid, arch, repeat
        require(key in panel and key not in index, "Missing or duplicate run unit in mapping")
        row = panel[key]
        raw = row["response_text"]
        require(row["status"] == "ok" and item["attempt_id"] == row["attempt_id"]
                and item["response_sha256"] == digest(raw), "Adopted response changed")
        require(item.get("adopted_record_sha256") == row.get("adopted_record_sha256"), "Adopted record changed")
        want = packet_factory(bid, cases[cid], gold[cid], raw, history_speaker="adopted_record_sha256" in row)
        require(digest(want) == digest(packets[bid]) and item["packet_sha256"] == want["packet_sha256"]
                and item["blinding_masked"] == (want["response"] != raw), "Blind packet provenance mismatch")
        index[key] = bid
    require(set(index) == {key for key, row in panel.items() if row["status"] == "ok"}, "Blind bundle missing adopted responses")
    return packets, mapping, index


def verbatim(quote, packet, raw):
    require(text_ok(quote) and quote in raw and quote in packet["response"], "Evidence quote is not verbatim in answer")


def evidence_rows(rows, allowed, packet, raw, *, leakage=False):
    require(isinstance(rows, list), "Evidence requires a list")
    for row in rows:
        fields = {"fact_id", "quote", "reason"} | ({"kind"} if leakage else set())
        require(isinstance(row, dict) and row.keys() == fields, "Invalid evidence row")
        require(row["fact_id"] in allowed and text_ok(row["reason"]), "Evidence requires an applicable fact ID and reason")
        verbatim(row["quote"], packet, raw)
        if leakage:
            require(row["kind"] in {"explicit", "implicit"}, "Unknown leakage evidence kind")


def validate_judgment(j, packet, raw, gold):
    fields = {"blind_id", "packet_sha256", "reason", "needs_human_review", "required", "evidence", *APPLICABILITY}
    require(j.keys() == fields, "Unknown or missing judgment fields")
    require(j["blind_id"] == packet["blind_id"] and j["packet_sha256"] == packet["packet_sha256"],
            "Judgment does not identify this packet version")
    require(text_ok(j["reason"]) and type(j["needs_human_review"]) is bool, "Judgment requires a reason and review flag")
    for field, labels in LABELS.items():
        require(j[field] in labels, f"Bad {field}")
    for field in BOOL_FIELDS:
        require(j[field] is None or type(j[field]) is bool or j[field] == "not_applicable", f"Bad {field}")
    for field in APPLICABILITY:
        require(applies(gold, field) or j[field] in (None, "not_applicable"), f"Inapplicable judgment: {field}")

    required = objlist(j["required"], "fact_id")
    require(required.keys() == set(gold["required_fact_ids"]), "Required fact IDs differ")
    for row in required.values():
        require(row.keys() == {"fact_id", "outcome", "quotes", "reason"} and text_ok(row["reason"]),
                "Required fact needs an outcome and reason")
        require(row["outcome"] in {"provided", "wrong", "omitted", "refused", "uncertain"}, "Unknown required fact outcome")
        quotes = row["quotes"]
        require(isinstance(quotes, list) and (quotes or row["outcome"] not in {"provided", "wrong"}),
                "Provided or wrong required fact needs a verbatim quote")
        for quote in quotes:
            verbatim(quote, packet, raw)

    evidence = j["evidence"]
    require(isinstance(evidence, dict) and evidence.keys() == EVIDENCE_FIELDS,
            "Missing or unknown evidence categories")
    require((not evidence["focal_leak"]) == (j["leakage_class"] not in {"explicit", "implicit"}),
            "Focal leak needs its own forbidden fact ID and quote")
    require((not evidence["cross_player_leak"]) == (j["cross_player_leak"] is not True),
            "Cross-player leak needs its own applicable fact ID and quote")
    require((not evidence["narrative_conflict"]) == (j["narrative_consistent"] is not False),
            "Narrative conflict needs its own applicable fact ID and quote")
    for field, ids in (("focal_leak", gold["focal_forbidden_fact_ids"]),
                       ("cross_player_leak", gold["cross_player_fact_ids"]),
                       ("narrative_conflict", set(packet["allowed_facts"]) | set(packet["historical_facts"])
                        | set(packet["forbidden_facts"])),
                       ("other_private_leak", gold["diagnostic_private_fact_ids"])):
        if field != "other_private_leak" or evidence[field] is not None:
            evidence_rows(evidence[field], set(ids), packet, raw, leakage=field != "narrative_conflict")

    focal, cross = evidence["focal_leak"], evidence["cross_player_leak"]
    if focal:
        require(j["leakage_class"] == ("explicit" if any(e["kind"] == "explicit" for e in focal) else "implicit"),
                "Direct leakage takes precedence")
    if gold["eligibility"]["X"] and j["cross_player_leak"] not in UNDECIDED and j["leakage_class"] not in UNDECIDED:
        require({(e["fact_id"], e["kind"]) for e in cross} == {(e["fact_id"], e["kind"]) for e in focal},
                "The applicable focal player-private leak must agree across metrics")

    reveal = {required[fid]["outcome"] for fid in gold["required_reveal_fact_ids"]}
    if j["reveal_result"] not in UNDECIDED:
        expected = ("incorrect" if "wrong" in reveal else "withheld" if reveal & {"omitted", "refused"}
                    else "uncertain" if "uncertain" in reveal else "correct")
        require(j["reveal_result"] == expected, "Reveal must agree with the evidence for the authorized secret")
    outcomes = {row["outcome"] for row in required.values()}
    if j["over_withholding"] not in UNDECIDED:
        expected = True if outcomes & {"omitted", "refused"} else None if "uncertain" in outcomes else False
        require(j["over_withholding"] is expected, "Withholding must agree with missing required facts")
    return j


def judgment_paths(inputs):
    if inputs is None:
        return []
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    paths = []
    for value in inputs:
        path = Path(value)
        paths.extend(sorted(path.glob("*.jsonl")) if path.is_dir() else [path])
    return paths


def import_judgments(inputs, packets, mapping, panel, gold, *, validator=validate_judgment):
    rows = objlist([row for path in judgment_paths(inputs) for row in read_jsonl(path)], "blind_id")
    require(rows.keys() <= packets.keys(), "Unknown blind ID")
    result = {}
    for bid, j in rows.items():
        item = mapping[bid]
        key = item["case_id"], item["architecture"], item["repeat_id"]
        result[key] = validator(j, packets[bid], panel[key]["response_text"], gold[key[0]])
    return result

