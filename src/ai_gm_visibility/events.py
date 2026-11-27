"""Accept only trusted scripted events; model text is never an event source."""

from copy import deepcopy

from .schemas import require
from .visibility import visible_ids


def apply_event(world, state, verified_event):
    e = deepcopy(verified_event)
    common = {"event_id", "world_id", "world_revision", "actor_id", "event_type"}
    require(common <= set(e), "Missing event fields")
    require(isinstance(e["event_id"], str) and e["event_id"], "Invalid event ID")
    require(e["actor_id"] in world["actors"], "Unknown event actor")
    require(e["world_id"] == world["world_id"] and e["world_revision"] == world["revision"], "Stale event/world")
    known = visible_ids(world, state, e["actor_id"])
    if e["event_id"] in state.accepted_events:
        require(state.accepted_events[e["event_id"]] == e, "Conflicting event ID")
        return deepcopy(state)
    if e["event_type"] == "share_fact":
        require(set(e) == common | {"fact_id", "recipient_id"}, "Invalid sharing fields")
        require(e["recipient_id"] in world["actors"], "Unknown sharing recipient")
        require(e["fact_id"] in known, "Sharing source has no access")
        effects = [(e["fact_id"], e["recipient_id"])]
    else:
        require(set(e) == common | {"target_id", "outcome"}, "Invalid event fields")
        require(e["event_type"] in {"read_document", "investigation_succeeded"}, "Unknown event type")
        operable = {entity for f in world["facts"] if f["fact_id"] in known for entity in f["entities"]}
        require(e["target_id"] in operable, "Target is not exposed to actor")
        rules = [r for r in world["reveal_rules"]
                 if r["event_type"] == e["event_type"] and r["target_id"] == e["target_id"]
                 and r["required_outcome"] == e["outcome"]]
        require(bool(rules), "No approved successful rule")
        require(all(set(r["requires_fact_ids"]) <= known for r in rules), "Unmet prerequisite")
        effects = [(fid, e["actor_id"]) for r in rules for fid in r["grant_fact_ids"]]
    updated = deepcopy(state)
    for fid, actor in effects:
        updated.grants[fid].add(actor)
    updated.accepted_events[e["event_id"]] = e
    updated.state_revision += 1
    updated.event_cursor += 1
    return updated


def replay(world, events):
    from .visibility import initial_state
    state = initial_state(world)
    for event in events:
        state = apply_event(world, state, event)
    return state
