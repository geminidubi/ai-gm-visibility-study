"""Execute the same generation loop for event replays and fixed snapshots."""

import subprocess
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .architectures import build_input
from .events import apply_event
from .g5_records import PACKET_FIELDS, RECORD_VERSION, dispatch_payload
from .llm.g5_ledger import compare_efforts
from .llm.mock import MockModel
from .llm.openai_backend import BASE_URL, MODEL, PRICING, TOKEN_FIELDS, OpenAIBudget, OpenAIModel
from .llm.openai_ledger import LEDGER_PATH, MAX_BUDGET_USD, MAX_REQUESTS, OpenAILedger
from .logging_io import append_jsonl, digest, file_hash, git_revision, write_json
from .report import write_comparison
from .retrieval import RetrievalError
from .schemas import load_cases, load_world, require
from .snapshot_cases import ARCHITECTURES, DATASET_VERSION, HISTORY_POLICY, context, load_snapshots
from .visibility import initial_state, snapshot, visible_ids


def validate_config(config):
    require(config["mode"] == "dev", "Formal execution blocked: dataset, model, rubric and budget are not approved")
    provider = config["provider"]
    require(provider in {"mock", "openai"}, "Unsupported provider")
    require(config["paid_calls_enabled"] is (provider == "openai"), "Paid calls require the OpenAI development config and provider")
    require(config["architectures"] == list(ARCHITECTURES), "All three architectures required")
    if config.get("dataset_version") == DATASET_VERSION:
        require(provider == "mock" and config.get("split") == "dev" and config.get("formal_approved") is False
                and config.get("budget_usd") is None, "G5 permits only offline development runs")
        require(not ({"world_path", "gold_path"} & config.keys()), "G5 generation config excludes gold and legacy world paths")
        require(config["history_policy"] == HISTORY_POLICY and config["retrieval"] == "clause_bm25_v1",
                "Unknown G5 history or retrieval policy")
    else:
        require("dataset_version" not in config and config["history_policy"] == "fixed_actor_scoped_empty_v1",
                "Unsupported history policy")
    require(type(config["repeats"]) is int and config["repeats"] > 0, "Invalid repeats")
    require(type(config["max_attempts"]) is int and 1 <= config["max_attempts"] <= 3, "Invalid retry limit")
    require(type(config["top_k"]) is int and config["top_k"] > 0, "Invalid top_k")
    if provider == "openai":
        settings = config["openai"]
        require(settings["model"] == MODEL and settings["reasoning_effort"] == "none", "Unsupported development model or reasoning effort")
        require(settings["store"] is False, "OpenAI responses must use store=false")
        require(type(settings["max_output_tokens"]) is int and 16 <= settings["max_output_tokens"] <= 1024, "Invalid development output limit")
        require(type(settings["timeout_seconds"]) is int and 1 <= settings["timeout_seconds"] <= 60, "Invalid development timeout")
        require(type(settings["max_retries"]) is int and settings["max_retries"] == 0, "SDK retries must be disabled")
        require(config["max_attempts"] == 1, "OpenAI development requires one attempt per turn")
        budget = config["budget_usd"]
        require(type(budget) in {int, float} and 0 < budget <= MAX_BUDGET_USD, "Invalid G4 budget limit")
        limit = config["request_limit"]
        require(type(limit) is int and 1 <= limit <= MAX_REQUESTS, "Invalid G4 cumulative request limit")


def run(config, output_root="runs", run_id=None, backend=None, simulated_ledger=":memory:"):
    validate_config(config)
    if config["provider"] == "mock":
        model = backend if backend is not None else MockModel(config["mock_mode"])
        return _run(config, output_root, run_id, model, synthetic=True)
    if backend is not None:
        with OpenAILedger(simulated_ledger, config["request_limit"], config["budget_usd"]) as ledger:
            return _run(config, output_root, run_id, backend, synthetic=True, ledger=ledger)
    model = OpenAIModel(config["openai"])
    try:
        # A custom report directory must not reset the production allowance.
        with OpenAILedger(LEDGER_PATH, config["request_limit"], config["budget_usd"]) as ledger:
            return _run(config, output_root, run_id, model, synthetic=False, ledger=ledger)
    finally:
        model.close()


def _turns(config, world, cases):
    for repeat in range(1, config["repeats"] + 1):
        for architecture in config["architectures"]:
            state = initial_state(world) if world is not None else None
            for case in cases:
                if world is None:
                    case_world, case_state, active, entries = context(case)
                    yield repeat, architecture, case, {}, case_state, case_world, active, entries
                    continue
                before = snapshot(world, state)
                for event in case["events_before"]:
                    state = apply_event(world, state, event)
                yield repeat, architecture, case, before, state, world, None, ()


def snapshot_packet(config, case, architecture, *, retrieval=None, top_k=None, history_speaker=True):
    world, state, active, history = context(case)
    return build_input(architecture, world, state, case["actor_id"], case["query"],
                       history=history, top_k=config["top_k"] if top_k is None else top_k,
                       prompt_dir=config["prompt_dir"], active_ids=active, as_of=case["as_of"],
                       retrieval=config["retrieval"] if retrieval is None else retrieval,
                       history_speaker=history_speaker)


def planned_units(config, cases):
    return [
        {field: case[field] for field in ("case_id", "world_id", "family_id", "pair_id")}
        | {"architecture": architecture, "repeat_id": repeat}
        for repeat in range(1, config["repeats"] + 1)
        for architecture in config["architectures"]
        for case in cases
    ]


def _run(config, output_root, run_id, model, synthetic, ledger=None, pilot=None):
    # Gold is never loaded by the generation path.
    is_g5 = config.get("dataset_version") == DATASET_VERSION
    world = None if is_g5 else load_world(config["world_path"])
    cases = (pilot.cases if pilot is not None else load_snapshots(config["cases_path"], "dev") if is_g5
             else load_cases(config["cases_path"], world))
    paid = config["provider"] == "openai"
    budget = OpenAIBudget(config["budget_usd"], config["openai"]["max_output_tokens"]) if paid else None
    parameters = model.parameters if paid else {"mock_mode": config["mock_mode"]}
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    require(Path(run_id).name == run_id and run_id not in {".", ".."}, "Invalid run ID")
    directory = Path(output_root) / run_id
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    sources = sorted(Path("src/ai_gm_visibility").rglob("*.py"))
    hashes = {str(p): file_hash(p) for p in sources}
    data_paths = (config["cases_path"],) if is_g5 else (config["world_path"], config["cases_path"], config["gold_path"])
    hashes.update({p: file_hash(p) for p in (*data_paths, "uv.lock")})
    hashes.update({str(p): file_hash(p) for p in sorted(Path(config["prompt_dir"]).glob("*.txt"))})
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    manifest = {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "stage": "G5" if is_g5 else "G4" if paid else "G0-G3",
        "synthetic": synthetic, "formal_result": False,
        "config": config, "config_hash": digest(config), "file_hashes": hashes,
        "code_commit": git_revision(), "code_dirty": bool(dirty.stdout.strip()),
        "requested_model": model.model_id, "effective_parameters": parameters,
        "language": "zh-Hant", "resume_supported": pilot is not None,
        "max_attempts": config["max_attempts"], "budget_usd": config["budget_usd"] if paid else None,
    }
    if paid:
        manifest.update(endpoint=BASE_URL + "/responses", sdk_version=model.sdk_version,
                        client_options=model.client_options, pricing_usd_per_million_tokens=PRICING)
        if pilot is not None:
            manifest.update(g5_limits_at_start=pilot.ledger.summary(), batch_size=pilot.batch_size)
        else:
            manifest["g4_limits_at_start"] = ledger.summary()
    manifest["configuration_fingerprint"] = digest({
        "config": config, "hashes": hashes, "model": model.model_id, "parameters": parameters,
    })
    if is_g5:
        units = planned_units(config, cases)
        manifest.update(planned_units=units, planned_units_sha256=digest(units), record_version=RECORD_VERSION)
    manifest_path = directory / "run_manifest.json"
    write_json(manifest_path, manifest)
    manifest_hash = file_hash(manifest_path) if is_g5 else None
    records_path = directory / "records.jsonl"
    records_path.touch(exist_ok=False)
    counts = {"ok": 0, "failed": 0, "attempts": 0}
    stop = None
    final_records = {}
    returned_models = set()
    previously_reserved = 0
    for repeat, architecture, case, before, state, turn_world, active, history in _turns(config, world, cases):
        key = (repeat, case["case_id"], architecture)
        retrieval_failure = None
        try:
            request = (pilot.packets[key[1:]] if pilot is not None else
                       build_input(architecture, turn_world, state, case["actor_id"], case["query"],
                                   history=history, top_k=config["top_k"], prompt_dir=config["prompt_dir"],
                                   active_ids=active, as_of=case.get("as_of"), retrieval=config.get("retrieval")))
        except RetrievalError as exc:
            retrieval_failure = str(exc)
            request = dict.fromkeys(("messages", "history_sources", *PACKET_FIELDS))
            request["retrieval_status"] = "failed"
        payload = dispatch_payload(config, request) if is_g5 and retrieval_failure is None else None
        payload_hash = digest(payload) if payload is not None else None
        if pilot is not None:
            if counts["attempts"] >= pilot.batch_size:
                break
            effort = config["openai"]["reasoning_effort"]
            expected_hash, quote = pilot.expected[effort, *key[1:]]
            require(payload_hash == expected_hash, "G5 dispatch changed after preflight")
            number, blocked = pilot.ledger.reserve(effort, *key[1:], records_path, manifest_hash, payload_hash, quote)
            if blocked == "already_reserved":
                previously_reserved += 1
                continue
        elif paid:
            quote = budget.quote(request["messages"])
            number, blocked = (None, "context_limit") if quote is None else ledger.reserve(
                run_id, repeat, case["case_id"], architecture, quote,
            )
        if paid:
            if blocked:
                stop = {
                    "reason": blocked, "repeat_id": repeat, "case_id": case["case_id"],
                    "architecture": architecture, "next_estimate_usd": float(quote) if quote is not None else None,
                }
                break
        if not is_g5:
            append_jsonl(directory / "events.jsonl", {
                "architecture": architecture, "repeat_id": repeat, "case_id": case["case_id"],
                "before": before, "after": snapshot(turn_world, state),
                "events": case["events_before"], "state_revision": state.state_revision,
                "accepted_event_ids": sorted(state.accepted_events), "event_cursor": state.event_cursor,
            })
        allowed = visible_ids(turn_world, state, case["actor_id"])
        if is_g5:
            allowed &= active
        for attempt in range(1, config["max_attempts"] + 1):
            record = {
                "run_id": run_id, "case_id": case["case_id"], "world_id": turn_world["world_id"],
                "scenario_id": case.get("scenario_id"), "turn_id": case.get("turn_index"),
                "actor_id": case["actor_id"], "architecture": architecture,
                "repeat_id": repeat, "attempt_id": attempt,
                "state_revision": state.state_revision, "events_applied": case.get("events_before", []),
                "visible_fact_ids": sorted(allowed),
                "rendered_messages": request["messages"],
                "allowed_candidate_ids": request["allowed_candidate_ids"],
                "selected_fact_ids": request["selected_fact_ids"],
                "input_source_fact_ids": request["input_source_fact_ids"],
                "requested_model": model.model_id, "returned_model": None,
                "parameter_effective_values": parameters,
                **dict.fromkeys(TOKEN_FIELDS),
                "estimated_cost": None, "response_text": None, "response_metadata": None,
                "status": "error", "error_type": None,
                "retry_reason": None if attempt == 1 else "previous_attempt_failed",
                "synthetic": synthetic,
            }
            if is_g5:
                record.update({field: case[field] for field in ("family_id", "pair_id", "as_of", "dataset_version")})
                record.update({field: request[field] for field in (*PACKET_FIELDS, "history_sources")})
                record.update(generation_request=deepcopy(payload), generation_request_sha256=payload_hash)
            if retrieval_failure is None:
                if pilot is not None:
                    _generate(model.generate_payload, payload, record)
                else:
                    _generate(model.generate, request, record)
            else:
                record.update(error_type="retrieval_failure", retrieval_error_type=retrieval_failure)
            if paid:
                metadata = record["response_metadata"] or {}
                tokens = metadata.get("usage") or {}
                record.update(
                    {field: tokens.get(field) for field in TOKEN_FIELDS},
                    provider_parameters=metadata.get("provider_parameters", {}),
                )
                safety_stop = budget.account(record, quote)
                reason = safety_stop or metadata.get("stop_reason")
                if pilot is not None:
                    record["g5_request_number"] = number
                    replaced = pilot.ledger.replaces_number(number)
                    if replaced is not None:
                        record["g5_replaces_request_number"] = replaced
                    reason = reason or _pilot_stop(record, parameters)
                else:
                    record["g4_request_number"] = number
                    ledger.settle(number, record, safety_stop)
                if reason:
                    stop = {"reason": reason, "repeat_id": repeat, "case_id": case["case_id"], "architecture": architecture}
            if record["returned_model"]:
                returned_models.add(record["returned_model"])
            append_jsonl(records_path, record)
            if pilot is not None:
                reason = pilot.ledger.settle(number, record, reason)
                if reason:
                    stop = {"reason": reason, "repeat_id": repeat, "case_id": case["case_id"], "architecture": architecture}
            counts["attempts"] += 1
            if record["status"] == "ok" or retrieval_failure is not None:
                break
        final_records[key] = record
        counts["ok" if record["status"] == "ok" else "failed"] += 1
        if stop:
            break
    if paid:
        counts.update(
            budget.summary(), stop=stop, returned_models=sorted(returned_models),
            planned=config["repeats"] * len(config["architectures"]) * len(cases),
        )
        if pilot is not None:
            counts.update(g5_totals=pilot.ledger.summary(), previously_reserved=previously_reserved)
        else:
            counts["g4_totals"] = ledger.summary()
        counts["unattempted"] = counts["planned"] - counts["ok"] - counts["failed"]
    if is_g5:
        counts.update(planned=len(units), unattempted=len(units) - len(final_records))
        write_json(directory / "record_seal.json",
                   {"run_manifest": manifest_hash, "raw_attempts": file_hash(records_path)})
    if pilot is not None:
        counts["g5_comparison"] = compare_efforts(pilot.ledger.audit(pilot.expected))
    write_json(directory / "summary.json", dict(counts, synthetic=synthetic, formal_result=False))
    write_comparison(directory / "comparison.md", config, cases, final_records, stop, synthetic)
    return directory, counts


def _pilot_stop(record, parameters):
    if record["returned_model"] not in {None, record["requested_model"]}:
        record.update(status="error", error_type="returned_model_mismatch")
        return "returned_model_mismatch"
    observed = record["provider_parameters"]
    for field in ("reasoning", "max_output_tokens", "store", "truncation"):
        if field not in observed:
            continue
        value = observed[field]
        if field == "reasoning" and isinstance(value, dict):
            value = {"effort": value.get("effort")}
        if value != parameters[field]:
            record.update(status="error", error_type="provider_parameters_mismatch")
            return "provider_parameters_mismatch"
    if record["error_type"] == "missing_or_invalid_usage":
        return "missing_or_invalid_usage"
    return None


def _generate(generate, request, record):
    start = time.monotonic()
    try:
        result = dict(generate(request), synthetic=record["synthetic"])
        text = result.get("response_text")
        record.update(response_text=text, response_metadata=result, returned_model=result.get("returned_model"))
        if result.get("error_type"):
            record["error_type"] = result["error_type"]
        else:
            require(isinstance(text, str) and text.strip(), "Empty response")
            require(result["finish_reason"] == "stop", "Truncated response")
            record["status"] = "ok"
    except Exception as exc:
        # Exception messages and HTTP bodies can include credentials.
        record["error_type"] = type(exc).__name__
    record["latency_ms"] = round((time.monotonic() - start) * 1000, 3)
