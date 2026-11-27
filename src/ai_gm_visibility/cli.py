"""Run from the repository root. OpenAI requires explicit opt-in."""

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path

from .evaluation import score_automatic
from .events import apply_event
from .logging_io import file_hash, read_jsonl, write_json
from .runner import run, validate_config
from .schemas import load_cases, load_gold, load_json, load_world, require
from .visibility import initial_state, visible_ids


def main(argv=None):
    parser = argparse.ArgumentParser(description="角色可見資訊控制的開發階段工具")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-data")
    check.add_argument("--split", choices=["dev"], default="dev")
    check.add_argument("--config", default="configs/dev.json")
    execute = commands.add_parser("run")
    execute.add_argument("--config", default="configs/dev.json")
    execute.add_argument("--provider", default="mock")
    execute.add_argument("--all", action="store_true", required=True)
    execute.add_argument("--output-root", default="runs")
    for name in ("inspect", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--run", required=True)
        command.add_argument("--output-root", default="runs")
        if name == "inspect":
            command.add_argument("--case", required=True)
    g5 = commands.add_parser("g5", help="G5 候選資料與離線盲評")
    operations = g5.add_subparsers(dest="operation", required=True)
    validate = operations.add_parser("validate")
    validate.add_argument("--data", default="data/g5")
    pilot = operations.add_parser("pilot", help="預檢 G5 開發小試。實際執行需要另行授權")
    pilot.add_argument("--effort", choices=["none", "low"])
    pilot.add_argument("--authorize-paid", action="store_true")
    pilot.add_argument("--batch-size", type=int, default=36)
    pilot.add_argument("--output-root", default="runs")
    operations.add_parser("pilot-status", help="唯讀對帳 G5 小試與恢復紀錄")
    recover = operations.add_parser("pilot-recovery", help="離線登記已核准的 75 次連線恢復")
    recover.add_argument("--authorize-limit-75", action="store_true", required=True)
    study = operations.add_parser("study", help="正式與 RP 實驗的離線預檢、獨立額度及對帳")
    study.add_argument("scope", choices=["formal", "rp"])
    study.add_argument("action", choices=["plan", "status", "run", "collect"])
    study.add_argument("--effort", choices=["none", "low"], required=True)
    study.add_argument("--max-output-tokens", type=int, default=2048)
    study.add_argument("--budget-usd")
    study.add_argument("--batch-size", type=int)
    study.add_argument("--approval")
    study.add_argument("--authorize-paid", action="store_true")
    study.add_argument("--output")
    for name in ("export", "assess", "import-work", "resume-work", "work-progress"):
        for prefix in ("", "rp-"):
            operation = operations.add_parser(prefix + name)
            operation.add_argument("--run", required=True)
            operation.add_argument("--output-root", default="runs")
            if not prefix:
                operation.add_argument("--gold", required=True)
            if name in {"export", "assess", "resume-work"}:
                operation.add_argument("--output", required=True)
            if name != "export":
                operation.add_argument("--bundle", required=True)
            if name in {"export", "resume-work"}:
                operation.add_argument("--batch-size", type=int)
                operation.add_argument("--max-batch-bytes", type=int)
            if name == "import-work":
                operation.add_argument("--input", action="append", required=True)
                operation.add_argument("--model", required=True)
                operation.add_argument("--reviewer", default="ChatGPT Work")
            if name == "assess":
                if not prefix:
                    operation.add_argument("--primary")
                operation.add_argument("--human", action="append")
                operation.add_argument("--adjudications", action="append")
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        if args.command == "g5":
            output = _g5_operation(args)
            if output.get("import_status") == "COMMITTED_WITH_REJECTIONS":
                exit_code = 3
            if args.operation in {"pilot", "study"} and (output.get("failed") or output.get("stop")):
                exit_code = 2
        elif args.command == "check-data":
            config = load_json(args.config)
            validate_config(config)
            require("dataset_version" not in config, "G5 data validation uses g5 validate")
            world = load_world(config["world_path"])
            cases = load_cases(config["cases_path"], world)
            gold = load_gold(config["gold_path"], cases, world)
            state = initial_state(world)
            for case in cases:
                for event in case["events_before"]:
                    state = apply_event(world, state, event)
                require(visible_ids(world, state, case["actor_id"]) == set(gold[case["case_id"]]["expected_visible_ids"]), "Replayed visibility disagrees with gold")
            output = {"status": "ok", "worlds": 1, "turns": len(cases), "gold_status": "candidate_not_human_approved"}
        elif args.command == "run":
            config = load_json(args.config)
            config["provider"] = args.provider
            directory, counts = run(config, args.output_root)
            synthetic = config["provider"] == "mock"
            output = {
                "directory": str(directory), "comparison": str(directory / "comparison.md"),
                **counts, "synthetic": synthetic, "formal_result": False,
            }
            if not synthetic and (counts["failed"] or counts["stop"]):
                exit_code = 2
        else:
            require(Path(args.run).name == args.run and args.run not in {".", ".."}, "Invalid run ID")
            directory = Path(args.output_root) / args.run
            records = read_jsonl(directory / "records.jsonl")
            if args.command == "inspect":
                output = [r for r in records if r["case_id"] == args.case]
                require(bool(output), "Case not found")
            else:
                manifest = load_json(directory / "run_manifest.json")
                require(manifest["stage"] != "G5", "G5 runs use g5 export and g5 assess")
                config = manifest["config"]
                for path in (config["world_path"], config["cases_path"], config["gold_path"]):
                    require(file_hash(path) == manifest["file_hashes"][path], "Evaluation data changed since generation")
                world = load_world(config["world_path"])
                cases = load_cases(config["cases_path"], world)
                gold = load_gold(config["gold_path"], cases, world)
                scores = [score_automatic(r, gold[r["case_id"]]) for r in records]
                path = directory / ("automatic-" + uuid.uuid4().hex[:8] + ".json")
                write_json(path, {
                    "synthetic": manifest["synthetic"], "formal_result": False,
                    "semantic_status": "NOT_RUN", "scores": scores,
                })
                output = {
                    "saved": str(path), "attempts_scored": len(scores),
                    "synthetic": manifest["synthetic"], "formal_result": False, "semantic_status": "NOT_RUN",
                }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return exit_code
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _g5_operation(args):
    if args.operation == "study":
        from .g5_study import collect, dispatch, prepare, status

        if args.action == "run":
            require(args.authorize_paid and args.approval, "Study is locked. Paid dispatch requires --authorize-paid and matching --approval")
        plan = prepare(args.scope, args.effort, args.budget_usd, args.max_output_tokens)
        if args.action == "plan":
            return plan[-1]
        if args.action == "status":
            return status(plan)
        if args.action == "collect":
            require(args.output, "Provide a new study review directory with --output")
            return collect(plan, Path(args.output))
        return dispatch(plan, args.batch_size if args.batch_size is not None else plan[-1]["dispatch_batch_size"],
                        approval=load_json(args.approval))
    if args.operation in {"pilot-status", "pilot-recovery"}:
        from .g5_pilot import pilot_ledger_status

        return pilot_ledger_status(authorize_limit_75=args.operation == "pilot-recovery")
    if args.operation == "pilot":
        from .g5_pilot import pilot_ledger_status, run_pilot

        if not args.authorize_paid:
            return pilot_ledger_status(preflight_only=True)
        require(args.effort is not None, "Paid G5 pilot requires one explicit effort per batch")
        directory, counts = run_pilot(args.effort, args.output_root, authorize_paid=True, batch_size=args.batch_size)
        return {"directory": str(directory), "effort": args.effort, **counts}

    # Imports are restricted to evaluation commands; generation works without gold files.
    from .g5_evidence import load_evidence, validate_design
    from .g5_review import assess_run, export_run, work_operation
    from .snapshot_cases import load_snapshots

    if args.operation == "validate":
        root = Path(args.data)
        dev = load_snapshots(root / "dev_cases.jsonl", "dev")
        formal = load_snapshots(root / "formal_candidate_cases.jsonl", "formal_candidate")
        return validate_design(dev, formal, load_evidence(root / "dev_gold.jsonl", dev),
                               load_evidence(root / "formal_candidate_gold.jsonl", formal))
    require(Path(args.run).name == args.run and args.run not in {".", ".."}, "Invalid run ID")
    run_dir = Path(args.output_root) / args.run
    operation = args.operation.removeprefix("rp-")
    batch_options = {}
    if operation in {"export", "resume-work"}:
        for name, value in (("batch_size", args.batch_size), ("max_bytes", args.max_batch_bytes)):
            if value is not None:
                require(value > 0, "Batch size and byte limit must be positive")
                batch_options[name] = value
    if args.operation.startswith("rp-"):
        from .g5_rp_review import score

        return score(operation, run_dir, Path(args.output) if hasattr(args, "output") else None,
                     Path(args.bundle) if hasattr(args, "bundle") else None, inputs=getattr(args, "input", None),
                     model=getattr(args, "model", None), reviewer=getattr(args, "reviewer", None),
                     human=getattr(args, "human", None), adjudications=getattr(args, "adjudications", None),
                     batch_options=batch_options)
    gold = Path(args.gold)
    if args.operation == "export":
        return export_run(run_dir, gold, Path(args.output), **batch_options)
    bundle = Path(args.bundle)
    if args.operation == "assess":
        return assess_run(run_dir, gold, bundle, Path(args.output),
                          primary=args.primary, human=args.human, adjudications=args.adjudications)
    if args.operation == "resume-work":
        return work_operation(args.operation, run_dir, gold, bundle,
                              output=Path(args.output), batch_options=batch_options)
    if args.operation == "import-work":
        return work_operation(args.operation, run_dir, gold, bundle, inputs=args.input,
                              model=args.model, reviewer=args.reviewer)
    return work_operation(args.operation, run_dir, gold, bundle)


if __name__ == "__main__":
    raise SystemExit(main())
