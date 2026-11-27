"""Match G5 execution attempts to the predeclared generation units."""

from pathlib import Path

from .g5_records import RECORD_VERSION, reconcile_attempt
from .logging_io import append_jsonl, digest, file_hash, read_jsonl
from .runner import planned_units, snapshot_packet
from .schemas import load_json, require
from .snapshot_cases import DATASET_VERSION, load_snapshots, projection, text_ok

UNIT_FIELDS = ("case_id", "architecture", "repeat_id")


def unit_key(row):
    return tuple(row[field] for field in UNIT_FIELDS)


def adopt_attempts(units, attempts, run_id, max_attempts, synthetic, *, config, cases, version=RECORD_VERSION):
    planned = {unit_key(row): row for row in units}
    require(len(planned) == len(units), "Duplicate planned unit")
    snapshots = {case["case_id"]: case for case in cases}
    grouped = {}
    for line, row in enumerate(attempts, 1):
        try:
            key = unit_key(row)
            require(type(row["repeat_id"]) is int and key in planned
                    and row["run_id"] == run_id and row["synthetic"] is synthetic,
                    "Attempt outside run plan")
            require(all(row[field] == planned[key][field] for field in ("world_id", "family_id", "pair_id")),
                    "Attempt case provenance mismatch")
            number = row["attempt_id"]
            require(type(number) is int and 1 <= number <= max_attempts, "Invalid attempt number")
            require(row["status"] == "error" or (row["status"] == "ok" and text_ok(row["response_text"])),
                    "Invalid attempt outcome")
            case = snapshots[row["case_id"]]
            if row.get("retrieval_status") == "failed":
                packet = {"allowed_candidate_ids": sorted(projection(case)[1])}
            else:
                packet = snapshot_packet(config, case, row["architecture"], history_speaker=version == RECORD_VERSION)
            reconcile_attempt(row, config, case, packet, version)
            group = grouped.setdefault(key, {})
            require(number not in group, "Duplicate attempt")
            group[number] = row
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"records.jsonl row {line}: {exc}") from exc
    panel = {}
    for key, unit in planned.items():
        group = grouped.get(key, {})
        if not group:
            panel[key] = dict(unit, run_id=run_id, status="unattempted", attempt_count=0, attempt_id=None,
                              retrieval_status="not_run" if unit["architecture"] == "C2" else "not_applicable",
                              selected_fact_ids=None, response_text=None, synthetic=synthetic)
            continue
        order = sorted(group)
        require(order == list(range(1, len(order) + 1)), "Missing preceding attempt")
        require(all(group[i]["status"] == "error" and group[i]["retrieval_status"] != "failed" for i in order[:-1]),
                "Unexpected retry after completed generation or retrieval failure")
        adopted = group[order[-1]]
        panel[key] = dict(adopted, attempt_count=len(order))
        if version == RECORD_VERSION:
            panel[key]["adopted_record_sha256"] = digest(adopted)
    return panel


def load_panel(directory):
    manifest_path = directory / "run_manifest.json"
    records_path = directory / "records.jsonl"
    manifest = load_json(manifest_path)
    config = manifest["config"]
    provenance = (config["mode"], config["split"], manifest["synthetic"], manifest["formal_result"])
    require(manifest["stage"] == "G5" and config["dataset_version"] == DATASET_VERSION
            and provenance in {("dev", "dev", True, False), ("dev", "dev", False, False),
                               ("formal", "formal_candidate", False, True)},
            "Expected a versioned G5 run")
    if manifest["formal_result"]:
        require(config["formal_approved"] is True, "Formal run must have been approved")
    require(manifest["config_hash"] == digest(config), "Run config changed")
    path = config["cases_path"]
    require(file_hash(path) == manifest["file_hashes"][path], "Run case data changed")
    hashes = {key.replace("\\", "/"): value for key, value in manifest["file_hashes"].items()}
    for name in ("common", "c0", "c1", "c2"):
        prompt = Path(config["prompt_dir"]) / f"{name}.txt"
        require(file_hash(prompt) == hashes.get(prompt.as_posix()), "Run prompts changed")
    version = manifest.get("record_version")
    seal_path = directory / "record_seal.json"
    require(version == RECORD_VERSION or (version is None and manifest["synthetic"]
            and config["provider"] == "mock" and not seal_path.exists()), "Unsupported G5 record format")
    cases = load_snapshots(path, config["split"])
    units = planned_units(config, cases)
    require(manifest["planned_units"] == units and manifest["planned_units_sha256"] == digest(units),
            "Run plan changed")
    try:
        panel = adopt_attempts(units, read_jsonl(records_path), manifest["run_id"],
                               manifest["max_attempts"], manifest["synthetic"], config=config, cases=cases, version=version)
        provenance = {
            "run_manifest": file_hash(manifest_path), "raw_attempts": file_hash(records_path),
            "synthetic": manifest["synthetic"], "formal_result": manifest["formal_result"],
        }
        if version == RECORD_VERSION:
            seal = load_json(seal_path)
            require(seal == {key: provenance[key] for key in ("run_manifest", "raw_attempts")},
                    "Run seal does not match the manifest or raw attempts")
            provenance["record_seal"] = file_hash(seal_path)
    except (ValueError, OSError) as exc:
        if version == RECORD_VERSION:
            try:
                append_jsonl(directory / "record_rejections.jsonl", {"reason": str(exc)})
            except OSError:
                pass
        raise
    return manifest, cases, panel, provenance

