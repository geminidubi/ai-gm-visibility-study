"""Preflight and dispatch only the frozen G5 development comparison."""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from .g5_records import dispatch_payload
from .llm.g5_ledger import BUDGET_USD, REQUEST_LIMIT, compare_efforts, open_g5_ledger
from .llm.openai_backend import MODEL, OpenAIBudget, OpenAIModel, response_parameters
from .llm.openai_ledger import LEDGER_PATH as G4_LEDGER_PATH
from .logging_io import digest
from .runner import _run, snapshot_packet
from .schemas import require
from .snapshot_cases import ARCHITECTURES, DATASET_VERSION, HISTORY_POLICY, load_snapshots

ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = ROOT / "runs/g5-dev-openai-budget.sqlite3"
EFFORTS = ("none", "low")
CASES_PATH = "data/g5/dev_cases.jsonl"
FROZEN_INPUTS = {
    CASES_PATH: "960f0a59437b55a7ec3a3fd3ed99ada3e526ce6ad4f21779cdb819bc650c8543",
    "prompts/c0.txt": "fc060256815e806fdfca2c5997daac09f3619d75058d2bed1ad5e5a8cc7de511",
    "prompts/c1.txt": "0020f11aa5f22b48a471a4eabeaf380a27a735b6dd925e802b43d32a409edb7b",
    "prompts/c2.txt": "41bc88b0ea67f9c47ab19331788a7f4f401baf3aede39eafc90fb6a0bdcc8a19",
    "prompts/common.txt": "a31eb38f59df2847e78ef64b2c62e5561876d96308b1e2ff16f3afa0bdef194a",
}
OUTPUT_LIMIT = 2048
TIMEOUT_SECONDS = 60
UNITS_PER_EFFORT = 36
RECOVERY_SOURCE_RUN = "20261125T184753Z-42789055"
RECOVERY_SOURCE_CASES = ("G5-c37232c7bc65", "G5-f76cfbb14935", "G5-970a502fdac1")


def pilot_config(effort):
    return {
        "dataset_version": DATASET_VERSION, "mode": "dev", "split": "dev", "formal_approved": False,
        "cases_path": CASES_PATH, "prompt_dir": "prompts", "architectures": list(ARCHITECTURES),
        "repeats": 1, "top_k": 10, "retrieval": "clause_bm25_v1", "history_policy": HISTORY_POLICY,
        "provider": "openai", "paid_calls_enabled": True, "max_attempts": 1,
        "request_limit": REQUEST_LIMIT, "budget_usd": float(BUDGET_USD),
        "openai": {"model": MODEL, "reasoning_effort": effort, "max_output_tokens": OUTPUT_LIMIT,
                   "store": False, "timeout_seconds": TIMEOUT_SECONDS, "max_retries": 0},
    }


@dataclass
class PilotBatch:
    cases: list
    packets: dict
    expected: dict
    ledger: object
    batch_size: int


def prepare_pilot():
    for path, expected in FROZEN_INPUTS.items():
        data = Path(path).read_bytes().replace(b"\r\n", b"\n")
        require(sha256(data).hexdigest() == expected, f"G5 pilot whitelist or prompt changed: {path}")
    config = pilot_config("none")
    cases = load_snapshots(CASES_PATH, "dev")
    require(len(cases) * len(ARCHITECTURES) == UNITS_PER_EFFORT, "Unexpected G5 development whitelist size")
    budget = OpenAIBudget(BUDGET_USD, OUTPUT_LIMIT)
    packets, quotes = {}, {}
    for case in cases:
        for arch in ARCHITECTURES:
            key = case["case_id"], arch
            packet = snapshot_packet(config, case, arch)
            quote = budget.quote(packet["messages"])
            require(quote is not None, "Pilot input exceeds the Standard short-context limit")
            packets[key], quotes[key] = packet, quote
    total = sum(quotes.values(), Decimal(0))
    require(total * len(EFFORTS) <= BUDGET_USD, "Full G5 pilot reservation exceeds the budget")
    expected = {
        (effort, *key): (digest(dispatch_payload(pilot_config(effort), packet)), quotes[key])
        for effort in EFFORTS for key, packet in packets.items()
    }
    report = {
        "status": "PREFLIGHT_OK", "paid_model_invoked": False,
        "case_ids": [case["case_id"] for case in cases], "architectures": list(ARCHITECTURES),
        "request_limit": REQUEST_LIMIT, "budget_usd": float(BUDGET_USD),
        "scenarios": [
            {"efforts": list(efforts), "requests": UNITS_PER_EFFORT * len(efforts),
             "conservative_quote_usd": float(total * len(efforts)),
             "sequential_timeout_seconds": TIMEOUT_SECONDS * UNITS_PER_EFFORT * len(efforts)}
            for efforts in (("none",), ("low",), EFFORTS)
        ],
        "settings": {effort: response_parameters(pilot_config(effort)["openai"]) for effort in EFFORTS},
    }
    return cases, packets, expected, report


def study_fingerprint(expected):
    # Keep the original research fingerprint. The higher cap is a separate ledger authorization.
    return digest({"config": [pilot_config(effort) for effort in EFFORTS],
                   "requests": [[*key, value, str(quote)] for key, (value, quote) in expected.items()]})


def pilot_ledger_status(*, authorize_limit_75=False, preflight_only=False):
    _, _, expected, preflight = prepare_pilot()
    if preflight_only and not LEDGER_PATH.is_file():
        return preflight
    with open_g5_ledger(LEDGER_PATH, study_fingerprint(expected), read_only=not authorize_limit_75,
                        must_exist=True) as ledger:
        observed = ledger.audit(expected)
        if authorize_limit_75:
            from .g5_panel import load_panel

            rows = ledger.rows()
            require([(row["number"], row["case_id"]) for row in rows] ==
                    list(enumerate(RECOVERY_SOURCE_CASES, start=1)), "Unexpected G5 recovery source attempts")
            source_directory = LEDGER_PATH.parent / RECOVERY_SOURCE_RUN
            require(all(Path(row["records_path"]).resolve() == source_directory / "records.jsonl" for row in rows),
                    "Unexpected G5 recovery source path")
            manifest, _, _, _ = load_panel(source_directory)
            require(manifest["run_id"] == RECOVERY_SOURCE_RUN and manifest["synthetic"] is False
                    and manifest["config"] == pilot_config("none"), "Unexpected G5 recovery source run")
            ledger.authorize_connection_recovery(RECOVERY_SOURCE_RUN, expected, observed)
            observed = ledger.audit(expected)
        totals = ledger.summary()
        status = "RECOVERY_AUTHORIZED" if authorize_limit_75 else "PREFLIGHT_OK" if preflight_only else "LEDGER_AUDITED"
        return {**preflight, "status": status, "request_limit": totals["request_limit"],
                "g5_totals": totals, "g5_comparison": compare_efforts(observed),
                "recovery_replacements_reserved": {
                    str(row["replaces_number"]): row["number"] for row in ledger.rows()
                    if ledger.recovery and row["replaces_number"] is not None
                },
                "frozen_72_request_quote_usd": preflight["scenarios"][-1]["conservative_quote_usd"]}


def run_pilot(effort, output_root="runs", *, authorize_paid=False, client=None, simulated_ledger=None,
              batch_size=UNITS_PER_EFFORT, run_id=None):
    require(effort in EFFORTS, "Choose one G5 effort per batch")
    require(type(batch_size) is int and 1 <= batch_size <= UNITS_PER_EFFORT, "Invalid G5 pilot batch size")
    require((client is None and authorize_paid is True and simulated_ledger is None)
            or (client is not None and not authorize_paid and simulated_ledger is not None),
            "Live G5 pilot requires explicit paid authorization; offline SDK tests require a separate ledger")
    if client is not None:
        protected = (LEDGER_PATH, ROOT / G4_LEDGER_PATH, Path(str(LEDGER_PATH) + ".dispatch"))
        require(Path(simulated_ledger).resolve() not in {path.resolve() for path in protected},
                "Simulation cannot access a live ledger")
    cases, packets, expected, _ = prepare_pilot()
    config = pilot_config(effort)
    with open_g5_ledger(LEDGER_PATH if client is None else simulated_ledger, study_fingerprint(expected)) as ledger:
        observed = ledger.audit(expected)
        totals = ledger.summary()
        require(not totals["stop_reason"], "G5 pilot has a prior safety stop")
        if ledger.recovery and effort == "low":
            require(sum(key[0] == "none" and row["status"] == "ok" for key, row in observed.items())
                    == UNITS_PER_EFFORT, "Finish and reconcile all 36 recovered none units before low")
        config["request_limit"] = totals["request_limit"]
        model = OpenAIModel(config["openai"], client=client)
        try:
            pilot = PilotBatch(cases, packets, expected, ledger, batch_size)
            return _run(config, output_root, run_id, model, synthetic=client is not None, pilot=pilot)
        finally:
            model.close()
