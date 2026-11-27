"""The only authority for actor access; unknown identities fail closed."""

from dataclasses import dataclass, field


@dataclass
class VisibilityState:
    world_id: str
    world_revision: int
    grants: dict[str, set[str]]
    state_revision: int = 0
    accepted_events: dict[str, dict] = field(default_factory=dict)
    event_cursor: int = 0


def initial_state(world):
    return VisibilityState(world["world_id"], world["revision"], {
        f["fact_id"]: set(f["initial_readers"]) for f in world["facts"]
    })


def visible_ids(world, state, trusted_actor_id):
    if state.world_id != world["world_id"] or state.world_revision != world["revision"]:
        raise ValueError("State/world version mismatch")
    if trusted_actor_id not in world["actors"]:
        return set()
    return {fid for fid, readers in state.grants.items()
            if "PUBLIC" in readers or trusted_actor_id in readers}


def snapshot(world, state):
    return {a: sorted(visible_ids(world, state, a)) for a in world["actors"]}
