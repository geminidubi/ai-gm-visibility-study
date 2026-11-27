"""G5 snapshot validation and adaptation to the existing visibility pipeline."""

from .architectures import HistoryEntry, visible_history
from .logging_io import read_jsonl
from .schemas import require
from .visibility import initial_state, visible_ids

DATASET_VERSION = "g5-candidate-v0.2"
HISTORY_POLICY = "fixed_audience_source_time_v2"
ARCHITECTURES = ("C0", "C1", "C2")
FACTORS = ("difficulty", "secret_type", "query_type", "authorization")
ROLES = {"PLAYER_A", "PLAYER_B", "GM"}
CASE_FIELDS = {"case_id", "world_id", "family_id", "pair_id", "dataset_version",
               "query", "split", "as_of", "actor_id", "facts", "history", *FACTORS}
FACT_FIELDS = {"fact_id", "content", "readers", "valid_from", "valid_to", "state_key", "state_value"}
HISTORY_FIELDS = {"history_id", "as_of", "actor_id", "audience", "text", "source_fact_ids", "observations"}


def objlist(sequence, key):
    require(isinstance(sequence, list), f"{key}: need list")
    out = {}
    for item in sequence:
        require(isinstance(item, dict), f"{key}: need object")
        k = item.get(key)
        require(isinstance(k, str) and k and k not in out, f"invalid/duplicate {key}: {k}")
        out[k] = item
    return out


def list_ids(items, name):
    require(isinstance(items, list) and all(isinstance(x, str) for x in items), name)
    require(len(set(items)) == len(items), f"{name} duplicates")
    return set(items)


def as_int(value, name):
    require(type(value) is int and value >= 0, name)
    return value


def text_ok(s):
    return isinstance(s, str) and bool(s.strip())


def current(fact, at):
    end = fact.get("valid_to")
    return fact.get("valid_from", 0) <= at and (end is None or at < end)


def timeline_state(facts, at):
    result = {}
    for f in facts.values():
        key = f.get("state_key")
        if key and current(f, at):
            value = f["state_value"]
            require(key not in result or result[key] == value,
                    f"conflicting fact versions for {key} at {at}")
            result[key] = value
    return result


def validate_case(case):
    require(set(case) == CASE_FIELDS, "snapshot fields must exclude gold fields")
    require(case["dataset_version"] == DATASET_VERSION, "snapshot dataset version")
    require(case["pair_id"] == f'{case["family_id"]}/{case["query_type"]}', "snapshot pair key")
    for k in ("case_id", "world_id", "family_id", "query") + FACTORS:
        require(text_ok(case.get(k)), f"missing/empty {k}")
    require(case.get("split") in {"dev", "formal_candidate"}, "bad split")
    require(case["difficulty"] in {"easy", "medium", "hard"}, "bad difficulty")
    require(case["authorization"] in {"authorized", "unauthorized"}, "bad authorization")
    at = as_int(case.get("as_of"), "snapshot as_of")
    require(case.get("actor_id") in ROLES - {"GM"}, "query actor unknown")
    facts = objlist(case.get("facts"), "fact_id")
    require(facts, "empty fact set")
    for f in facts.values():
        require(set(f) <= FACT_FIELDS, "snapshot fact fields must exclude gold fields")
        f_id = f["fact_id"]
        require(text_ok(f.get("content")), f"{f_id}: no content")
        readers = list_ids(f.get("readers"), f"{f_id}: readers")
        require(readers <= ROLES | {"PUBLIC"}, f"{f_id}: unknown reader")
        start = as_int(f.get("valid_from", 0), f"{f_id}: valid_from")
        end = f.get("valid_to")
        require(start <= at, f"{f_id}: future fact in snapshot package")
        require(end is None or as_int(end, f"{f_id}: valid_to") > start,
                f"{f_id}: nonpositive version interval")
        if "state_key" in f or "state_value" in f:
            require(text_ok(f.get("state_key")) and text_ok(f.get("state_value")),
                    f"{f_id}: state annotation incomplete")
    boundaries = {at} | {f.get("valid_from", 0) for f in facts.values()}
    boundaries |= {f["valid_to"] for f in facts.values()
                   if f.get("valid_to") is not None and f["valid_to"] <= at}
    for t in boundaries:
        timeline_state(facts, t)
    hist = objlist(case.get("history"), "history_id")
    for h in hist.values():
        require(set(h) <= HISTORY_FIELDS, "snapshot history fields must exclude gold fields")
        h_id = h["history_id"]
        t = as_int(h.get("as_of"), f"{h_id}: as_of")
        require(t <= at, f"{h_id}: future history")
        require(h.get("actor_id") in ROLES, f"{h_id}: history actor unknown")
        aud = list_ids(h.get("audience"), f"{h_id}: audience")
        require(aud and aud <= ROLES | {"PUBLIC"}, f"{h_id}: unknown/empty audience")
        require(text_ok(h.get("text")), f"{h_id}: no text")
        ids = list_ids(h.get("source_fact_ids"), f"{h_id}: source_fact_ids")
        require(ids and ids <= facts.keys(), f"{h_id}: missing/unresolved history provenance")
        require(all(current(facts[f], t) for f in ids), f"{h_id}: source invalid at historical time")
        states = timeline_state(facts, t)
        observations = h.get("observations", [])
        require(isinstance(observations, list), f"{h_id}: observations shape")
        for obs in observations:
            require(isinstance(obs, dict) and set(obs) == {"state_key", "state_value"} and text_ok(obs.get("state_key"))
                    and text_ok(obs.get("state_value")), f"{h_id}: invalid observation")
            key, value = obs["state_key"], obs["state_value"]
            require(states.get(key) == value
                    and any(facts[f].get("state_key") == key
                            and facts[f].get("state_value") == value for f in ids),
                    f"{h_id}: contradiction or missing structured support for {key}")
    return facts, hist


def context(case):
    facts, history = validate_case(case)
    world = {
        "world_id": case["world_id"], "revision": 2, "actors": sorted(ROLES - {"GM"}),
        "facts": [dict(fact_id=f["fact_id"], content=f["content"], entities=[],
                       initial_readers=f["readers"])
                  for f in facts.values()],
    }
    state = initial_state(world)
    active_ids = {f["fact_id"] for f in facts.values() if current(f, case["as_of"])}
    entries = tuple(
        HistoryEntry(h["actor_id"], h["text"], tuple(h["source_fact_ids"]), tuple(h["audience"]),
                     history_id=h["history_id"], as_of=h["as_of"])
        for h in sorted(history.values(), key=lambda row: (row["as_of"], row["history_id"]))
    )
    return world, state, active_ids, entries


def projection(case):
    world, state, active, entries = context(case)
    allowed = visible_ids(world, state, case["actor_id"])
    history = visible_history(entries, world, allowed, case["actor_id"], audience_scoped=True)
    return active, active & allowed, history


def load_snapshots(path, split):
    cases = read_jsonl(path)
    require(cases, "Empty snapshot dataset")
    objlist(cases, "case_id")
    for case in cases:
        validate_case(case)
        require(case["split"] == split, "Unexpected snapshot split")
    return cases
