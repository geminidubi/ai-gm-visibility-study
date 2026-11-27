"""Explicit input assembly with source provenance and actor history gates."""

from dataclasses import dataclass
from pathlib import Path

from .retrieval import RetrievalError, rank_snapshot, retrieve
from .schemas import require
from .visibility import visible_ids


@dataclass(frozen=True)
class HistoryEntry:
    actor_id: str
    text: str
    source_fact_ids: tuple[str, ...]
    audience: tuple[str, ...]
    history_id: str | None = None
    as_of: int | None = None


def visible_history(history, world, allowed, actor, *, audience_scoped=False):
    entries = []
    facts = {f["fact_id"] for f in world["facts"]}
    audiences = set(world["actors"])
    if audience_scoped:
        audiences |= {"PUBLIC", "GM"}
    for h in history:
        require(isinstance(h, HistoryEntry), "History requires provenance")
        require(h.source_fact_ids and set(h.source_fact_ids) <= facts, "Invalid history provenance")
        require(set(h.audience) <= audiences, "Invalid history audience")
        audience_ok = actor in h.audience or (audience_scoped and "PUBLIC" in h.audience)
        if audience_ok and (audience_scoped or h.actor_id == actor) and set(h.source_fact_ids) <= allowed:
            entries.append(h)
    return entries


def build_input(architecture, world, state, actor, query, history=(), top_k=3, prompt_dir="prompts",
                *, active_ids=None, as_of=None, retrieval=None, history_speaker=True):
    require(architecture in {"C0", "C1", "C2"}, "Unknown architecture")
    require(actor in world["actors"], "Untrusted identity")
    allowed = visible_ids(world, state, actor)
    is_snapshot = active_ids is not None
    facts = [f for f in world["facts"] if not is_snapshot or f["fact_id"] in active_ids]
    candidates = [f for f in facts if f["fact_id"] in allowed]
    ranking = []
    if architecture == "C2":
        if is_snapshot:
            try:
                ranking = rank_snapshot(candidates, query, retrieval)
            except Exception as exc:
                raise RetrievalError(type(exc).__name__) from exc
            selected = [row["fact"] for row in ranking[:top_k]]
        else:
            selected = [x["fact"] for x in retrieve(candidates, query, top_k)]
    else:
        selected = facts
    entries = visible_history(history, world, allowed, actor, audience_scoped=is_snapshot)
    if architecture in {"C0", "C1"}:
        background = "本玩家目前已知：\n" + "\n".join(f["content"] for f in candidates)
    else:
        background = "僅使用下方已授權並檢索的證據。"
    evidence = []
    for f in selected:
        label = ""
        if architecture == "C1":
            label = " readers=" + ",".join(sorted(state.grants[f["fact_id"]]))
        evidence.append(f"[{f['fact_id']}{label}] {f['content']}")
    prompts = Path(prompt_dir)
    messages = [
        {"role": "system", "content": (prompts / "common.txt").read_text(encoding="utf-8")},
        {"role": "system", "content": (prompts / f"{architecture.lower()}.txt").read_text(encoding="utf-8")},
        {"role": "system", "content": f"可信玩家：{actor}\n{background}\n證據：\n" + "\n".join(evidence)},
    ]
    if is_snapshot:
        messages[2]["content"] = (
            f"作答時點：{as_of}。歷史只記當時狀態，與當前證據衝突時以當前證據為準。\n"
            + messages[2]["content"]
        )
    for h in entries:
        speaker = f"，說話者 {h.actor_id}" if history_speaker else ""
        label = f"核定角色歷史（時點 {h.as_of}{speaker}）：" if is_snapshot else "核定角色歷史："
        messages.append({"role": "system", "content": label + h.text})
    messages.append({"role": "user", "content": query})
    source_ids = {f["fact_id"] for f in selected} | {fid for h in entries for fid in h.source_fact_ids}
    if architecture == "C2":
        require(source_ids <= allowed, "C2 input contamination")
    packet = {
        "messages": messages, "actor_id": actor,
        "allowed_candidate_ids": sorted(f["fact_id"] for f in candidates),
        "selected_fact_ids": [f["fact_id"] for f in selected],
        "input_source_fact_ids": sorted(source_ids),
        "history_count": len(entries),
    }
    if is_snapshot:
        packet.update(
            candidate_fact_ids=[f["fact_id"] for f in (candidates if architecture == "C2" else facts)],
            history_ids=[h.history_id for h in entries],
            history_sources=[{"history_id": h.history_id, "actor_id": h.actor_id, "as_of": h.as_of,
                              "source_fact_ids": list(h.source_fact_ids)} for h in entries],
            retrieval_status="completed" if architecture == "C2" else "not_applicable",
            ranked_facts=[{"fact_id": row["fact"]["fact_id"], "score": row["score"]} for row in ranking],
        )
    return packet
