"""Export Work-ready batches and retain validated primary judgments across sessions."""

import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .g5_judging import RUBRIC, RUBRIC_VERSION, validate_judgment
from .g5_metrics import pending_fields
from .logging_io import digest, file_hash, write_json
from .schemas import require
from .snapshot_cases import text_ok

DEFAULT_BATCH_SIZE = 12
DEFAULT_BATCH_BYTES = 220_000
INSTRUCTIONS = Path("docs/g5-work-instructions.md")
STORE = "work_review_PRIVATE.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL, raw BLOB NOT NULL,
    model TEXT NOT NULL, reviewer TEXT NOT NULL, rubric_version TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id), line INTEGER NOT NULL,
    blind_id TEXT, status TEXT NOT NULL, detail TEXT, conflict_judgment TEXT,
    PRIMARY KEY (delivery_id, line)
);
CREATE TABLE IF NOT EXISTS accepted (
    blind_id TEXT PRIMARY KEY, delivery_id INTEGER NOT NULL, line INTEGER NOT NULL,
    judgment TEXT NOT NULL, FOREIGN KEY (delivery_id, line) REFERENCES receipts(delivery_id, line)
);
"""


def export_batches(output, packets, *, batch_size=DEFAULT_BATCH_SIZE, max_bytes=DEFAULT_BATCH_BYTES,
                   rubric=RUBRIC, rubric_version=RUBRIC_VERSION, instructions=INSTRUCTIONS):
    require(type(batch_size) is int and batch_size > 0 and type(max_bytes) is int and max_bytes > 0,
            "Batch size and byte limit must be positive")
    output.mkdir(parents=True, exist_ok=False)
    entries, lines, size = [], [], 0

    def save_batch():
        name = f"batch-{len(entries) + 1:03}.jsonl"
        path = output / name
        path.write_bytes(b"".join(lines))
        entries.append({"file": name, "count": len(lines), "utf8_bytes": size,
                        "over_byte_target": size > max_bytes, "sha256": file_hash(path)})

    for packet in packets:
        line = (json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if lines and (len(lines) >= batch_size or size + len(line) > max_bytes):
            save_batch()
            lines, size = [], 0
        lines.append(line)
        size += len(line)
    if lines:
        save_batch()
    shutil.copyfile(rubric, output / "judge_rubric.md")
    shutil.copyfile(instructions, output / "work_instructions.md")
    summary = {"rubric_version": rubric_version, "rubric_sha256": file_hash(rubric),
               "packet_count": sum(entry["count"] for entry in entries),
               "target_batch_count": batch_size, "max_jsonl_bytes": max_bytes, "batches": entries}
    write_json(output / "manifest.json", summary)
    return summary


def store_identity(bundle):
    return file_hash(bundle / "mapping_PRIVATE.json")


@contextmanager
def work_store(bundle, *, writable=False):
    path = bundle / STORE
    if not writable and not path.exists():
        yield None
        return
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        if writable:
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        elif db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone() is None:
            yield None
            return
        identity = store_identity(bundle)
        with db:
            if writable:
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('bundle', ?)", (identity,))
            row = db.execute("SELECT value FROM metadata WHERE key='bundle'").fetchone()
            require(row is not None and row["value"] == identity, "Work judgments belong to another bundle")
            yield db
    finally:
        db.close()


def read_accepted(db):
    if db is None:
        return {}, {}
    judgments, origins = {}, {}
    rows = db.execute("""
        SELECT a.blind_id, a.line, a.judgment, d.name, d.sha256, d.model,
               d.reviewer, d.rubric_version, d.imported_at
        FROM accepted a JOIN deliveries d ON a.delivery_id=d.id
    """)
    for row in rows:
        record = dict(row)
        bid = record.pop("blind_id")
        judgments[bid] = json.loads(record.pop("judgment"))
        origins[bid] = record
    return judgments, origins


def unit_by_blind(index):
    return {bid: key for key, bid in index.items()}


def persisted_judgments(bundle, index, packets, panel, gold, *, validator=validate_judgment):
    by_blind = unit_by_blind(index)
    with work_store(bundle) as db:
        if db is not None:
            db.execute("BEGIN")
        accepted, origins = read_accepted(db)
        require(accepted.keys() <= by_blind.keys(), "Stored Work judgment outside blind bundle")
        counts = (dict(db.execute("SELECT status, COUNT(*) FROM receipts GROUP BY status").fetchall())
                  if db is not None else {})
        latest_rejections = ([] if db is None else [dict(row) for row in db.execute("""
            SELECT r.blind_id, r.status, r.detail, r.line, d.name
            FROM receipts r JOIN deliveries d ON r.delivery_id=d.id
            WHERE r.status IN ('invalid', 'unknown', 'duplicate', 'conflict')
              AND (r.blind_id IS NULL OR NOT EXISTS
                   (SELECT 1 FROM accepted a WHERE a.blind_id=r.blind_id))
            ORDER BY d.id, r.line
        """)])
        alternatives = ([] if db is None else db.execute("""
            SELECT DISTINCT blind_id, conflict_judgment
            FROM receipts WHERE status='conflict' ORDER BY blind_id, conflict_judgment
        """).fetchall())
    primary, provenance = {}, {}
    for bid, judgment in accepted.items():
        key = by_blind[bid]
        primary[key] = validator(judgment, packets[bid], panel[key]["response_text"], gold[key[0]])
        provenance[key] = origins[bid]
    conflicts = {}
    for row in alternatives:
        bid = row["blind_id"]
        conflicts.setdefault(by_blind[bid], []).append(json.loads(row["conflict_judgment"]))
    return primary, provenance, counts, latest_rejections, conflicts


def unique_json_object(pairs):
    fields = {}
    for key, value in pairs:
        require(key not in fields, f"Duplicate JSON member: {key}")
        fields[key] = value
    return fields


def read_submission(path):
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw, [(0, None, None, "Expected UTF-8 JSONL")]
    entries = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=unique_json_object)
            require(isinstance(value, dict) and text_ok(value.get("blind_id")), "Missing blind_id")
        except (ValueError, TypeError) as exc:
            entries.append((number, None, None, str(exc)))
        else:
            entries.append((number, value["blind_id"], value, None))
    return raw, entries or [(0, None, None, "Empty judgment file")]


def import_work(bundle, paths, model, reviewer, packets, index, panel, gold, *, validator=validate_judgment,
                rubric_version=RUBRIC_VERSION):
    require(text_ok(model) and text_ok(reviewer), "Record the Work model and reviewer for each import")
    by_blind = unit_by_blind(index)
    submissions = [(path, *read_submission(path)) for path in paths]
    counts = Counter(bid for _, _, entries in submissions for _, bid, _, _ in entries if bid is not None)
    receipts = []
    with work_store(bundle, writable=True) as db:
        existing, _ = read_accepted(db)
        for path, raw, entries in submissions:
            delivery_id = db.execute(
                "INSERT INTO deliveries (name,sha256,raw,model,reviewer,rubric_version,imported_at) VALUES (?,?,?,?,?,?,?)",
                (path.name, hashlib.sha256(raw).hexdigest(), raw, model, reviewer, rubric_version,
                 datetime.now(timezone.utc).isoformat()),
            ).lastrowid
            for line, bid, judgment, error in entries:
                status, detail = "accepted", None
                if error:
                    status, detail = "invalid", error
                elif counts[bid] > 1:
                    status, detail = "duplicate", "Repeated blind ID in this import"
                elif bid not in packets:
                    status, detail = "unknown", "Blind ID does not belong to this bundle"
                else:
                    key = by_blind[bid]
                    try:
                        validator(judgment, packets[bid], panel[key]["response_text"], gold[key[0]])
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        status, detail = "invalid", str(exc)
                    else:
                        if bid in existing:
                            if digest(existing[bid]) == digest(judgment):
                                status, detail = "already_accepted", "Unchanged valid judgment kept"
                            else:
                                status, detail = "conflict", "Original valid judgment kept for human review"
                db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?)",
                           (delivery_id, line, bid, status, detail,
                            json.dumps(judgment, ensure_ascii=False, sort_keys=True) if status == "conflict" else None))
                if status == "accepted":
                    db.execute("INSERT INTO accepted VALUES (?,?,?,?)",
                               (bid, delivery_id, line, json.dumps(judgment, ensure_ascii=False)))
                    existing[bid] = judgment
                receipts.append({"file": path.name, "line": line, "blind_id": bid, "status": status, "detail": detail})
    return receipts


def work_progress(index, primary, panel, gold, import_counts, invalid, conflicts):
    missing = [bid for key, bid in index.items() if key not in primary]
    uncertain = [
        {"blind_id": index[key], "fields": sorted(pending_fields(gold[key[0]], j)),
         "other_private_leak_pending": j["evidence"]["other_private_leak"] is None,
         "uncertain_required_ids": [item["fact_id"] for item in j["required"] if item["outcome"] == "uncertain"]}
        for key, j in primary.items()
        if pending_fields(gold[key[0]], j) or j["evidence"]["other_private_leak"] is None
        or any(item["outcome"] == "uncertain" for item in j["required"])
    ]
    return {
        "planned_generation_units": len(panel), "eligible_for_review": len(index),
        "generation_unavailable": len(panel) - len(index), "work_received_valid": len(primary),
        "work_pending": len(missing), "work_pending_blind_ids": missing, "work_uncertain": uncertain,
        "import_receipt_counts": import_counts, "unresolved_import_errors": invalid,
        "conflicting_work_blind_ids": [index[key] for key in conflicts],
    }

