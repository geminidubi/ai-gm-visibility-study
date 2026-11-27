"""Outcome-independent sampling and balanced execution order for the G5 studies."""

from collections import Counter, defaultdict
from itertools import product
from random import Random

from .schemas import require
from .snapshot_cases import ARCHITECTURES

FORMAL_SAMPLE_SEED = 202611260518
RP_SAMPLE_SEED = 202611260519
FORMAL_BATCH_SIZE = 120


def family_cohorts(cases):
    families = defaultdict(dict)
    for case in cases:
        families[case["secret_type"]][case["difficulty"]] = case["family_id"]
    require(len(families) == 5 and all(len(group) == 3 for group in families.values()),
            "Expected one family per difficulty and secret type")
    return [[families[kind][difficulty] for kind in sorted(families)]
            for difficulty in ("easy", "medium", "hard")]


def order_formal(units, cases):
    groups = family_cohorts(cases)
    ranks = {name: (cohort, rank) for cohort, families in enumerate(groups) for rank, name in enumerate(families)}
    ordered = sorted(units, key=lambda u: (ranks[u["family_id"]][0], u["repeat_id"],
                                          ranks[u["family_id"]][1], u["pair_id"], u["case_id"], u["architecture"]))
    for start in range(0, len(ordered), FORMAL_BATCH_SIZE):
        batch = ordered[start:start + FORMAL_BATCH_SIZE]
        require(len({u["case_id"] for u in batch}) == 40
                and set(Counter(u["case_id"] for u in batch).values()) == {3}
                and len({u["repeat_id"] for u in batch}) == 1,
                "Each formal cohort must retain complete authorization pairs and architectures")
    return ordered


def formal_slots(cases):
    rng = Random(FORMAL_SAMPLE_SEED)
    by_family = defaultdict(list)
    for case in sorted(cases, key=lambda row: row["case_id"]):
        by_family[case["family_id"]].append(case)
    families = sorted(by_family)
    masks = [mask for mask in product((False, True), repeat=3) if 0 < sum(mask) < 3]
    for _ in range(100000):
        assignment = {family: rng.choice(masks) for family in families}
        by_type = Counter((rows[0]["secret_type"], i, flag) for name, rows in by_family.items()
                          for i, flag in enumerate(assignment[name]))
        if all(sum(assignment[name][i] for name in families) in (7, 8) for i in range(3)) and len(by_type) == 30:
            break
    else:
        raise ValueError("No balanced formal authorization assignment for sampling")
    slots = []
    for i, arch in enumerate(ARCHITECTURES):
        repeats = [1, 2, 3] * 5
        rng.shuffle(repeats)
        for family, repeat in zip(families, repeats):
            auth = "authorized" if assignment[family][i] else "unauthorized"
            candidates = [case["case_id"] for case in by_family[family] if case["authorization"] == auth]
            rng.shuffle(candidates)
            slots.append([(cid, arch, repeat) for cid in candidates])
    require(len(slots) == 45 and all(len(slot) == 4 for slot in slots), "Invalid frozen formal sample")
    return slots


def rp_candidates(cases):
    rng = Random(RP_SAMPLE_SEED)
    candidates = [(case["case_id"], case["world"], case["fields"][0]["result"] == "成功。")
                  for case in sorted(cases, key=lambda row: row["case_id"])]
    options = [candidates.copy() for _ in ARCHITECTURES]
    for items in options:
        rng.shuffle(items)
    options = [list(zip(items, [arch] * len(items))) for items, arch in zip(options, ARCHITECTURES)]
    choices = sorted(product(*options), key=lambda group: (-len({case[1] for case, _ in group}),
                                                           -len({case[2] for case, _ in group})))
    return [[(case[0], arch, 1) for case, arch in group] for group in choices]


def choose_sample(scope, design, panel):
    """Take frozen replacements only after preferred units are known to have failed."""
    if scope == "rp":
        for group in design:
            if all(panel[key]["status"] == "ok" for key in group):
                return set(group)
        # If an architecture has no successful unit, keep the best available distinct-world group.
        return set(max(([key for key in group if panel[key]["status"] == "ok"] for group in design), key=len))
    selected = set()
    for slot in design:
        for key in slot:
            status = panel[key]["status"]
            if status == "ok":
                selected.add(key)
            if status in {"ok", "unattempted"}:
                break
    return selected
