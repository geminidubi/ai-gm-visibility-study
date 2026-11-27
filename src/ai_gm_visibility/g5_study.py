"""Plan, authorize, dispatch and reconcile the independent formal and RP studies."""

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from . import g5_rp
from .g5_judging import FORMAL_RUBRIC
from .g5_records import PACKET_FIELDS, RECORD_VERSION as REQUEST_VERSION, dispatch_payload, reconcile_attempt
from .g5_study_sampling import FORMAL_BATCH_SIZE, FORMAL_SAMPLE_SEED, RP_SAMPLE_SEED, formal_slots, order_formal, rp_candidates
from .llm.g5_ledger import open_g5_ledger
from .llm.openai_backend import MODEL, PRICING, TOKEN_FIELDS, OpenAIBudget, OpenAIModel, response_parameters
from .logging_io import append_jsonl, digest, file_hash, git_revision, read_jsonl, write_json
from .runner import _generate, _pilot_stop, planned_units, snapshot_packet
from .schemas import load_json, require
from .snapshot_cases import ARCHITECTURES, DATASET_VERSION, load_snapshots, text_ok

ROOT = Path(__file__).resolve().parents[2]
REVIEW_FILE = "study-review.json"
LIMITS = {"formal": (1080, "4.00"), "rp": (18, "0.10")}
RECORD_VERSION = "g5-study-v1"


def prepare(scope, effort, budget_usd=None, output_limit=2048):
    require(scope in LIMITS and effort in {"none", "low"}, "Choose a study and the pilot-selected effort")
    limit, suggested = LIMITS[scope]
    try:
        cap = Decimal(str(suggested if budget_usd is None else budget_usd)).normalize()
    except InvalidOperation as exc:
        raise ValueError("Use a numeric USD budget") from exc
    require(cap.is_finite() and cap > 0, "Use a positive, finite USD budget")
    require(type(output_limit) is int and 16 <= output_limit <= 2048, "Output limit must be 16–2048 tokens")
    if scope == "formal":
        config = load_json("configs/g5-formal.json")
        require(config["mode"] == "formal" and config["split"] == "formal_candidate"
                and config["dataset_version"] == DATASET_VERSION and config["repeats"] == 3
                and config["architectures"] == list(ARCHITECTURES) and config["prompt_dir"] == "prompts"
                and config["top_k"] == 10 and config["retrieval"] == "clause_bm25_v1"
                and config["history_policy"] == "fixed_audience_source_time_v2" and config["max_attempts"] == 1
                and config["paid_calls_enabled"] is False and config["formal_approved"] is False
                and not {"gold_path", "world_path"} & config.keys(), "Unexpected locked formal candidate config")
        config.pop("planning_only_openai")
        cases = load_snapshots(config["cases_path"], "formal_candidate")
        require(len(cases) == 120 and len({case["pair_id"] for case in cases}) == 60, "Expected the 120-case formal panel")
        packets = {(case["case_id"], arch): snapshot_packet(config, case, arch)
                   for case in cases for arch in ARCHITECTURES}
        evidence = Path("data/g5/formal_candidate_gold.jsonl")
        rubric = FORMAL_RUBRIC
        instructions = Path("docs/g5-work-instructions.md")
        sampling = formal_slots(cases)
    else:
        definitions = g5_rp.load_cases()
        config = {"mode": "dev", "split": "rp", "dataset_version": g5_rp.VERSION,
                  "cases_path": (g5_rp.DATA / "cases.json").as_posix(), "prompt_dir": "prompts",
                  "architectures": list(ARCHITECTURES), "top_k": g5_rp.TOP_K, "repeats": 1,
                  "max_attempts": 1, "formal_approved": False}
        cases = [{"case_id": c["case_id"], "world_id": c["world"], "family_id": c["world"], "pair_id": c["world"],
                  "as_of": c["as_of"], "actor_id": c["actor"], "dataset_version": g5_rp.VERSION}
                 for c in definitions["cases"]]
        packets = {(p["case_id"], p["architecture"]): p for c in definitions["cases"]
                   for p in g5_rp.assemble(c, definitions["worlds"][c["world"]])[0]}
        evidence = g5_rp.DATA / "reference.json"
        rubric = g5_rp.DATA / "review/judge-rubric.md"
        instructions = g5_rp.DATA / "review/work-instructions.md"
        sampling = rp_candidates(definitions["cases"])
    config.update(provider="openai", paid_calls_enabled=True, budget_usd=str(cap), request_limit=limit,
                  human_review_design_sha256=digest(sampling),
                  openai={"model": MODEL, "reasoning_effort": effort, "max_output_tokens": output_limit,
                          "timeout_seconds": 60, "max_retries": 0, "store": False})
    units = planned_units(config, cases)
    if scope == "formal":
        units = order_formal(units, cases)
    require(len(units) == limit, "Study panel does not match its cumulative request limit")
    budget = OpenAIBudget(cap, output_limit)
    distinct = {}
    for key, packet in packets.items():
        quote = budget.quote(packet["messages"])
        require(quote is not None, "A study input exceeds the Standard short-context limit")
        distinct[key] = digest(dispatch_payload(config, packet)), quote
    expected = {ledger_key(effort, unit): distinct[unit["case_id"], unit["architecture"]] for unit in units}
    total = sum(quote for _, quote in expected.values())
    paths = [Path(config["cases_path"]), evidence, rubric, instructions, Path("uv.lock"),
             Path("scripts/run_g5_approved.py")]
    paths += sorted(Path("src/ai_gm_visibility").rglob("*.py"))
    paths += sorted(Path(config["prompt_dir"]).glob("*.txt"))
    hashes = {p.as_posix(): file_hash(p) for p in paths}
    plan_id = digest({"scope": scope, "config": config, "hashes": hashes, "pricing": PRICING, "units": units,
                      "requests": [[*key, request, str(quote)] for key, (request, quote) in expected.items()]})
    report = {"scope": scope, "plan_id": plan_id, "paid_calls": 0, "approval_required": True, "requests": limit,
              "cases": len(cases), "repeats": config["repeats"], "architectures": list(ARCHITECTURES),
              "budget_usd": str(cap), "whole_study_reservation_usd": str(total), "fits_budget": total <= cap,
              "sequential_timeout_allowance_seconds": limit * config["openai"]["timeout_seconds"],
              "model": MODEL, "parameters": response_parameters(config["openai"]),
              "cases_sha256": hashes[config["cases_path"]], "evidence_sha256": hashes[evidence.as_posix()],
              "rubric_sha256": hashes[rubric.as_posix()], "pricing_usd_per_million_tokens": PRICING,
              "estimated_work_batches_at_12": (limit + 11) // 12,
              "dispatch_batch_size": FORMAL_BATCH_SIZE if scope == "formal" else limit,
              "human_sample_seed": FORMAL_SAMPLE_SEED if scope == "formal" else RP_SAMPLE_SEED,
              "human_review_design_sha256": config["human_review_design_sha256"],
              "minimum_random_human_reviews_if_all_succeed": 45 if scope == "formal" else 3}
    return config, cases, units, packets, expected, report


def ledger_key(effort, unit):
    return effort, f'{unit["case_id"]}#{unit["repeat_id"]}', unit["architecture"]


def approved(plan, receipt):
    report = plan[-1]
    require(receipt.get("approved") is True and receipt.get("scope") == report["scope"]
            and receipt.get("plan_id") == report["plan_id"], "Approval must explicitly match this study and plan ID")
    fields = ("approved_by", "pilot_assessment", "formal_dataset_and_rubric_decisions") if report["scope"] == "formal" else (
        "approved_by", "pilot_assessment")
    require(all(text_ok(receipt.get(field)) for field in fields), "Approval is missing the review and decision references")
    require(report["fits_budget"], "Full study reservation exceeds the approved cap. Replan before requesting approval")


def live_root(scope):
    return ROOT / "runs" / f"g5-{scope}-paid"


def open_ledger(plan, root, *, read_only=False):
    report = plan[-1]
    path = root / "budget.sqlite3"
    require(path.is_file() or not any(root.glob("batch-*")), "Study batches exist but ledger is missing")
    return open_g5_ledger(path, report["plan_id"], read_only=read_only,
                          limits=(report["requests"], Decimal(report["budget_usd"])))


def audit_study(plan, ledger):
    observed = ledger.audit(plan[4])
    effort = plan[0]["openai"]["reasoning_effort"]
    require(all(key == ledger_key(effort, row) for key, row in observed.items()), "Study ledger unit identity mismatch")
    return observed, ledger.summary()


def status(plan):
    report = plan[-1]
    root = live_root(report["scope"])
    if not root.exists():
        return {**report, "status": "NO_PAID_LEDGER"}
    with open_ledger(plan, root, read_only=True) as ledger:
        rows, totals = audit_study(plan, ledger)
    return {**report, "status": "LEDGER_AUDITED", "ledger": totals,
            "statuses": dict(Counter(row["status"] for row in rows.values()))}


def dispatch(plan, batch_size, *, approval=None, simulated_client=None, simulated_root=None):
    config, _, units, _, _, report = plan
    require(type(batch_size) is int and 1 <= batch_size <= len(units), "Invalid study batch size")
    synthetic = simulated_client is not None
    if synthetic:
        require(approval is None and simulated_root is not None, "Synthetic dispatch requires its own run directory")
        root = Path(simulated_root).resolve()
        require(root.name.startswith("simulation-") and not any(
            root.is_relative_to(live_root(scope).resolve()) for scope in LIMITS), "Separate the synthetic ledger from live data")
        require(report["fits_budget"], "Synthetic study reservation exceeds its cap")
    else:
        require(approval is not None and simulated_root is None, "Explicit study approval and paid authorization required")
        approved(plan, approval)
        require(batch_size == report["dispatch_batch_size"], "Batch size must match the frozen study plan")
        root = live_root(report["scope"])
    effort = config["openai"]["reasoning_effort"]
    with open_ledger(plan, root) as ledger:
        old, totals = audit_study(plan, ledger)
        require(not totals["stop_reason"] and not totals["unsettled_requests"],
                "Study is stopped or has unresolved dispatches. Preserve the ledger and reconcile before proceeding")
        # A process can exit after the last settlement and before sealing the batch.
        for path in {Path(row["records_path"]) for row in ledger.rows()}:
            seal = path.with_name("record_seal.json")
            if not seal.exists():
                write_json(seal, {"run_manifest": file_hash(path.with_name("run_manifest.json")),
                                  "raw_attempts": file_hash(path)})
        remaining = [unit for unit in units if ledger_key(effort, unit) not in old][:batch_size]
        if not remaining:
            return {"status": "ALL_UNITS_ATTEMPTED", "ledger": totals, "attempts": 0}
        model = OpenAIModel(config["openai"], client=simulated_client)
        try:
            result = dispatch_batch(plan, remaining, root, ledger, model, synthetic, approval)
        finally:
            model.close()
        return result


def dispatch_batch(plan, units, root, ledger, model, synthetic, approval):
    config, cases, _, packets, expected, report = plan
    by_id = {case["case_id"]: case for case in cases}
    effort = config["openai"]["reasoning_effort"]
    budget = OpenAIBudget(config["budget_usd"], config["openai"]["max_output_tokens"])
    run_id = "batch-" + uuid4().hex
    directory = root / run_id
    directory.mkdir(exist_ok=False)
    manifest = {"stage": "G5", "run_id": run_id, "scope": report["scope"], "plan_id": report["plan_id"],
                "record_version": RECORD_VERSION, "config": config, "batch_plan": units,
                "synthetic": synthetic, "approval": approval, "code_commit": git_revision(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sdk_version": model.sdk_version, "client_options": model.client_options,
                "effective_parameters": model.parameters, "ledger_at_start": ledger.summary()}
    manifest_path, records_path = directory / "run_manifest.json", directory / "records.jsonl"
    write_json(manifest_path, manifest)
    records_path.touch(exist_ok=False)
    manifest_hash = file_hash(manifest_path)
    counts = Counter()
    stop = None
    for unit in units:
        cid, arch = unit["case_id"], unit["architecture"]
        packet = packets[cid, arch]
        payload = dispatch_payload(config, packet)
        key = ledger_key(effort, unit)
        request_hash, quote = expected[key]
        require(digest(payload) == request_hash, "Study payload changed after preflight")
        number, stop = ledger.reserve(*key, records_path, manifest_hash, request_hash, quote)
        if stop:
            break
        case = by_id[cid]
        record = {**unit, **{name: case[name] for name in ("as_of", "actor_id", "dataset_version")},
                  **{name: packet[name] for name in (*PACKET_FIELDS, "history_sources")},
                  "run_id": run_id, "attempt_id": 1, "synthetic": synthetic, "status": "error",
                  "visible_fact_ids": packet["allowed_candidate_ids"], "rendered_messages": packet["messages"],
                  "generation_request": payload, "generation_request_sha256": request_hash,
                  "requested_model": model.model_id, "parameter_effective_values": model.parameters,
                  "g5_request_number": number, "response_text": None, "returned_model": None,
                  "response_metadata": None, "error_type": None}
        if "adjudication_sources" in packet:
            record["adjudication_sources"] = packet["adjudication_sources"]
        _generate(model.generate_payload, payload, record)
        metadata = record["response_metadata"] or {}
        usage = metadata.get("usage") or {}
        record.update({name: usage.get(name) for name in TOKEN_FIELDS},
                      provider_parameters=metadata.get("provider_parameters", {}))
        stop = budget.account(record, quote) or metadata.get("stop_reason") or _pilot_stop(record, model.parameters)
        append_jsonl(records_path, record)
        stop = ledger.settle(number, record, stop)
        counts[record["status"]] += 1
        if stop:
            break
    write_json(directory / "record_seal.json", {"run_manifest": manifest_hash, "raw_attempts": file_hash(records_path)})
    audit_study(plan, ledger)
    result = {"directory": str(directory), "synthetic": synthetic, "attempts": sum(counts.values()),
              "ok": counts["ok"], "failed": counts["error"], "stop": stop, "ledger": ledger.summary(),
              "finished_at": datetime.now(timezone.utc).isoformat(), **budget.summary()}
    write_json(directory / "summary.json", result)
    return result


def collect(plan, output, *, simulated_root=None, checkpoint=False):
    config, _, _, _, _, report = plan
    root = Path(simulated_root).resolve() if simulated_root is not None else live_root(report["scope"])
    with open_ledger(plan, root, read_only=True) as ledger:
        observed, totals = audit_study(plan, ledger)
        balanced_checkpoint = (checkpoint and report["scope"] == "formal"
                               and totals["requests_used"] % FORMAL_BATCH_SIZE == 0)
        require(observed and not totals["unsettled_requests"] and (
            totals["requests_remaining"] == 0 or totals["stop_reason"] or balanced_checkpoint),
            "Collect after the study is fully attempted or stopped and the ledger is reconciled")
        paths = sorted({row["records_path"] for row in ledger.rows()})
        batches = [{"records": path, "seal_sha256": file_hash(Path(path).with_name("record_seal.json"))} for path in paths]
        snapshot = {"scope": report["scope"], "plan_id": report["plan_id"], "config": config,
                    "synthetic": simulated_root is not None, "ledger": totals, "batches": batches,
                    "attempt_record_sha256": digest(sorted(digest(row) for row in observed.values()))}
        _, _, panel, _ = review_snapshot(snapshot, plan)
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / REVIEW_FILE, snapshot)
    return {"output": str(output), "planned": len(panel), "ledger": totals,
            "successful": sum(row["status"] == "ok" for row in panel.values())}


def review_snapshot(snapshot, plan):
    config, cases, units, packets, _, report = plan
    require(snapshot["plan_id"] == report["plan_id"] and snapshot["config"] == config, "Study settings or files changed")
    synthetic = snapshot["synthetic"]
    panel = {(u["case_id"], u["architecture"], u["repeat_id"]): {
        **u, "status": "unattempted", "attempt_count": 0, "retrieval_status": "not_run" if u["architecture"] == "C2"
        else "not_applicable", "response_text": None, "selected_fact_ids": None, "synthetic": synthetic} for u in units}
    by_id = {case["case_id"]: case for case in cases}
    digests, numbers = [], set()
    for batch in snapshot["batches"]:
        path = Path(batch["records"])
        seal_path = path.with_name("record_seal.json")
        manifest_path = path.with_name("run_manifest.json")
        require(batch["seal_sha256"] == file_hash(seal_path) and load_json(seal_path) == {
            "run_manifest": file_hash(manifest_path), "raw_attempts": file_hash(path)}, "Study record seal mismatch")
        manifest = load_json(manifest_path)
        require(manifest["plan_id"] == report["plan_id"] and manifest["config"] == config
                and manifest["scope"] == report["scope"] and manifest["synthetic"] is synthetic
                and manifest["record_version"] == RECORD_VERSION and manifest["run_id"] == path.parent.name,
                "Study manifest mismatch")
        if not synthetic:
            approved(plan, manifest["approval"])
        records = read_jsonl(path)
        require([{name: row[name] for name in units[0]} for row in records] == manifest["batch_plan"][:len(records)],
                "Attempt sequence differs from the batch plan")
        for row in records:
            key = row["case_id"], row["architecture"], row["repeat_id"]
            require(type(row["repeat_id"]) is int and key in panel and panel[key]["status"] == "unattempted"
                    and row["run_id"] == manifest["run_id"] and row["synthetic"] is synthetic
                    and type(row["attempt_id"]) is int and row["attempt_id"] == 1
                    and all(row[f] == panel[key][f] for f in ("world_id", "family_id", "pair_id"))
                    and row["g5_request_number"] not in numbers
                    and (row["status"] == "error" or (row["status"] == "ok" and text_ok(row["response_text"]))),
                    "Invalid or repeated study attempt")
            packet = packets[key[:2]]
            reconcile_attempt(row, config, by_id[key[0]], packet, REQUEST_VERSION)
            require(row.get("adjudication_sources") == packet.get("adjudication_sources"), "RP adjudication mismatch")
            raw_hash = digest(row)
            digests.append(raw_hash)
            numbers.add(row["g5_request_number"])
            panel[key] = dict(row, attempt_count=1, adopted_record_sha256=raw_hash)
    require(numbers == set(range(1, snapshot["ledger"]["requests_used"] + 1))
            and digest(sorted(digests)) == snapshot["attempt_record_sha256"], "Study review and cumulative ledger disagree")
    formal = report["scope"] == "formal" and not synthetic
    manifest = {"config": config, "synthetic": synthetic, "formal_result": formal}
    provenance = {"study_snapshot_sha256": digest(snapshot), "synthetic": synthetic, "formal_result": formal}
    return manifest, cases, panel, provenance


def load_review(directory, scope):
    snapshot = load_json(directory / REVIEW_FILE)
    require(snapshot["scope"] == scope, "Use the scoring workflow for this study")
    config = snapshot["config"]
    return review_snapshot(snapshot, prepare(scope, config["openai"]["reasoning_effort"], config["budget_usd"],
                                             config["openai"]["max_output_tokens"]))
