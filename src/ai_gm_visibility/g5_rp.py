"""Assemble RP v0.2 generation inputs from cases and current repository prompts."""

from pathlib import Path

from .architectures import HistoryEntry, build_input
from .schemas import load_json, require
from .snapshot_cases import ARCHITECTURES
from .visibility import initial_state, visible_ids

DATA = Path("data/g5-rp-dev/v0.2")
PROMPTS = Path("prompts")
VERSION = "g5-rp-dev-v0.2"
TOP_K = 10
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
        "world_id": case["world"], "revision": 2, "actors": list(ACTORS),
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
        if authorized(actor, row["audience"], row["sources"], prior & allowed):
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
                             active_ids=active, as_of=case["as_of"], top_k=TOP_K,
                             retrieval="clause_bm25_v1", prompt_dir=PROMPTS)
        packet["messages"][-1:-1] = shared
        source_ids = {fid for row in projected for fid in row["sources"]}
        packet["input_source_fact_ids"] = sorted(source_ids | set(packet["input_source_fact_ids"]))
        packet["adjudication_sources"] = [
            {"field_id": row["id"], "source_event_id": case["event"], "source_fact_ids": row["sources"]}
            for row in projected
        ]
        packets.append({"case_id": case["case_id"], "architecture": architecture, **packet})
    return packets, prior, allowed, outcomes[0]


def load_cases():
    definitions = load_json(DATA / "cases.json")
    require(definitions["dataset_version"] == VERSION, "Wrong RP version")
    require(len(definitions["worlds"]) == 3 and len(definitions["cases"]) == 6
            and len({case["case_id"] for case in definitions["cases"]}) == 6, "Expected three worlds and six cases")
    return definitions
