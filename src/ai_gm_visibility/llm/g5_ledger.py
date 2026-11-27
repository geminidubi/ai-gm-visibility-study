"""Track all G5 dispatch attempts and the authorized connection recovery."""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..logging_io import digest, file_hash, read_jsonl
from ..schemas import load_json, require

REQUEST_LIMIT = 72
RECOVERY_LIMIT = 75
BUDGET_USD = Decimal("2")
REQUEST_COLUMNS = """
    number INTEGER PRIMARY KEY,
    effort TEXT NOT NULL CHECK (effort IN ('none', 'low')),
    case_id TEXT NOT NULL,
    architecture TEXT NOT NULL CHECK (architecture IN ('C0', 'C1', 'C2')),
    records_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    quote_usd TEXT NOT NULL,
    cost_usd TEXT,
    status TEXT,
    error_type TEXT,
    truncated INTEGER,
    record_sha256 TEXT,
    stop_reason TEXT
"""


@contextmanager
def open_g5_ledger(path, fingerprint, *, read_only=False, must_exist=False, limits=None):
    path = Path(path)
    if read_only:
        require(path.is_file(), "G5 pilot ledger does not exist")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN")
                yield G5Ledger(db, fingerprint, initialize=False, limits=limits)
        return
    if must_exist:
        require(path.is_file(), "G5 pilot ledger does not exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    # A separate database holds a process-wide dispatch lock. Reservations in the
    # ledger can commit before sending, and an interrupted process releases the lock.
    with closing(sqlite3.connect(str(path) + ".dispatch", timeout=0)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            yield G5Ledger(db, fingerprint, limits=limits)


class G5Ledger:
    def __init__(self, db, fingerprint, *, initialize=True, limits=None):
        self.db = db
        self.request_limit, self.budget_usd = limits if limits is not None else (REQUEST_LIMIT, BUDGET_USD)
        if initialize:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS study (id INTEGER PRIMARY KEY CHECK (id=1), fingerprint TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO study VALUES (1, ?)", (fingerprint,))
                db.execute(f"""CREATE TABLE IF NOT EXISTS g5_requests (
                    {REQUEST_COLUMNS}, UNIQUE (effort, case_id, architecture)
                )""")
        study = db.execute("SELECT fingerprint FROM study WHERE id=1").fetchone()
        require(study is not None and study["fingerprint"] == fingerprint,
                "G5 pilot inputs or settings changed since the first batch")
        self.recovery = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='g5_recovery'").fetchone():
            require(limits is None, "Connection recovery belongs to the question pilot only")
            receipt = db.execute("SELECT * FROM g5_recovery WHERE id=1").fetchone()
            require(receipt is not None, "Missing G5 recovery receipt")
            self.recovery = dict(receipt)

    def authorize_connection_recovery(self, source_run, expected, observed):
        """Record the one-time exception while preserving all old row values."""
        require((self.request_limit, self.budget_usd) == (REQUEST_LIMIT, BUDGET_USD),
                "Connection recovery belongs to the question pilot only")
        require(self.recovery is None, "G5 connection recovery has already been authorized")
        rows = self.rows()
        require([row["number"] for row in rows] == [1, 2, 3]
                and all(row["effort"] == "none" and row["architecture"] == "C0" and row["status"] == "error"
                        and row["error_type"] == "api_connection_error" and row["cost_usd"] is None
                        and row["truncated"] == 0 and row["record_sha256"] for row in rows)
                and [row["stop_reason"] for row in rows] == [None, None, "consecutive_generation_errors"],
                "G5 recovery requires the original three connection errors and safety stop")
        require(len(observed) == 3 and all(record["run_id"] == source_run and record["returned_model"] is None
                    and record["response_text"] is None and (record["response_metadata"] or {}).get("request_id") is None
                    and (record["response_metadata"] or {}).get("http_status") is None for record in observed.values()),
                "G5 recovery requires the original run without an HTTP response")
        (path,) = {Path(row["records_path"]) for row in rows}
        seal = load_json(path.with_name("record_seal.json"))
        require(sum((quote for _, quote in expected.values()), Decimal(0))
                + sum((_amount(row["quote_usd"]) for row in rows), Decimal(0)) <= BUDGET_USD,
                "G5 recovery would exceed the approved budget")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("ALTER TABLE g5_requests RENAME TO g5_requests_original")
            self.db.execute(f"""CREATE TABLE g5_requests (
                {REQUEST_COLUMNS}, replaces_number INTEGER UNIQUE REFERENCES g5_requests(number)
            )""")
            self.db.execute("INSERT INTO g5_requests SELECT *, NULL FROM g5_requests_original")
            self.db.execute("DROP TABLE g5_requests_original")
            self.db.execute("""CREATE UNIQUE INDEX g5_original_unit ON g5_requests(effort, case_id, architecture)
                               WHERE replaces_number IS NULL""")
            self.db.execute("""CREATE TABLE g5_recovery (
                id INTEGER PRIMARY KEY CHECK (id=1), source_run TEXT NOT NULL,
                through_number INTEGER NOT NULL CHECK (through_number=3),
                request_limit INTEGER NOT NULL CHECK (request_limit=75),
                prior_stop TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                raw_attempts_sha256 TEXT NOT NULL, authorized_at_utc TEXT NOT NULL
            )""")
            self.db.execute("INSERT INTO g5_recovery VALUES (1, ?, 3, ?, ?, ?, ?, ?)",
                            (source_run, RECOVERY_LIMIT, rows[-1]["stop_reason"], seal["run_manifest"],
                             seal["raw_attempts"], datetime.now(timezone.utc).isoformat()))
        self.recovery = dict(self.db.execute("SELECT * FROM g5_recovery WHERE id=1").fetchone())

    def rows(self):
        return list(self.db.execute("SELECT * FROM g5_requests ORDER BY number"))

    def audit(self, expected):
        batches = {}
        observed = {}
        rows = self.rows()
        originals = {row["number"]: row for row in rows}
        if self.recovery:
            through = self.recovery["through_number"]
            require(set(range(1, through + 1)) <= originals.keys()
                    and originals[through]["stop_reason"] == self.recovery["prior_stop"],
                    "Original G5 safety stop or reservations are missing")
        for row in rows:
            key = row["effort"], row["case_id"], row["architecture"]
            require(key in expected and expected[key] == (row["request_sha256"], _amount(row["quote_usd"])),
                    "G5 ledger request does not match preflight")
            replacement = row["replaces_number"] if self.recovery else None
            if replacement is not None:
                require(1 <= replacement <= self.recovery["through_number"] < row["number"]
                        and replacement in originals and all(row[field] == originals[replacement][field]
                            for field in ("effort", "case_id", "architecture", "request_sha256", "quote_usd")),
                        "G5 recovery reservation does not match its original request")
            batches.setdefault(Path(row["records_path"]), {})[row["number"]] = row
        for path, reservations in batches.items():
            seal = path.with_name("record_seal.json")
            manifest = path.with_name("run_manifest.json")
            require(path.is_file() and manifest.is_file(), "G5 ledger batch records or manifest missing")
            manifest_hash = file_hash(manifest)
            require(all(row["manifest_sha256"] == manifest_hash for row in reservations.values()),
                    "G5 ledger batch manifest mismatch")
            if seal.exists():
                require(load_json(seal) == {"raw_attempts": file_hash(path),
                        "run_manifest": manifest_hash}, "G5 ledger batch seal mismatch")
            if self.recovery and min(reservations) <= self.recovery["through_number"]:
                require(seal.is_file() and manifest_hash == self.recovery["manifest_sha256"]
                        and file_hash(path) == self.recovery["raw_attempts_sha256"], "G5 recovery seal mismatch")
            records = {}
            for record in read_jsonl(path):
                number = record.get("g5_request_number")
                require(type(number) is int and number in reservations and number not in records,
                        "G5 batch record has no unique reservation")
                reservation = reservations[number]
                require(record.get("generation_request_sha256") == reservation["request_sha256"]
                        and record.get("status") == reservation["status"]
                        and record.get("g5_replaces_request_number") ==
                        (reservation["replaces_number"] if self.recovery else None)
                        and _amount(record.get("estimated_cost")) == _amount(reservation["cost_usd"]),
                        "G5 ledger metadata and batch record disagree")
                records[number] = digest(record)
                observed[number] = record
            require(records == {number: row["record_sha256"] for number, row in reservations.items()
                                if row["record_sha256"] is not None},
                    "G5 ledger and batch records disagree")
        adopted = {}
        for row in rows:
            key = row["effort"], row["case_id"], row["architecture"]
            if row["number"] in observed:
                adopted[key] = observed[row["number"]]
        return adopted

    def summary(self):
        rows = self.rows()
        estimated = sum((Decimal(row["cost_usd"]) for row in rows if row["cost_usd"] is not None), Decimal(0))
        reserved = sum((Decimal(row["quote_usd"]) for row in rows if row["cost_usd"] is None), Decimal(0))
        limit = self.recovery["request_limit"] if self.recovery else self.request_limit
        through = self.recovery["through_number"] if self.recovery else 0
        summary = {
            "request_limit": limit, "requests_used": len(rows),
            "requests_remaining": max(0, limit - len(rows)), "budget_usd": float(self.budget_usd),
            "estimated_cost_usd": float(estimated), "reserved_cost_usd": float(reserved),
            "unsettled_requests": sum(row["status"] is None for row in rows),
            "attempts_with_unknown_cost": sum(row["cost_usd"] is None for row in rows),
            "by_effort": {effort: sum(row["effort"] == effort for row in rows) for effort in ("none", "low")},
            "stop_reason": next((row["stop_reason"] for row in rows if row["number"] > through and row["stop_reason"]), None),
        }
        if self.recovery:
            summary["connection_recovery"] = {**self.recovery, "original_request_numbers": list(range(1, through + 1))}
        return summary

    def reserve(self, effort, case_id, architecture, path, manifest_hash, request_hash, quote):
        require(quote > 0, "Invalid G5 reservation quote")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.rows()
            limits = self.summary()
            if limits["stop_reason"]:
                return None, "prior_g5_safety_stop"
            matching = [row for row in rows if (row["effort"], row["case_id"], row["architecture"])
                        == (effort, case_id, architecture)]
            through = self.recovery["through_number"] if self.recovery else 0
            if matching and (len(matching) > 1 or matching[0]["number"] > through):
                return None, "already_reserved"
            if len(rows) >= limits["request_limit"]:
                return None, "request_limit_reached"
            used = sum((Decimal(row["cost_usd"] if row["cost_usd"] is not None else row["quote_usd"])
                        for row in rows), Decimal(0))
            if used + quote > self.budget_usd:
                return None, "budget_exhausted"
            previous = matching[0]["number"] if matching else None
            if previous is not None:
                require(request_hash == matching[0]["request_sha256"] and quote == _amount(matching[0]["quote_usd"]),
                        "G5 recovery request differs from its original reservation")
            column = ", replaces_number" if self.recovery else ""
            place = ", ?" if self.recovery else ""
            args = (effort, case_id, architecture, str(path.resolve()), manifest_hash, request_hash, str(quote))
            cursor = self.db.execute(f"""
                INSERT INTO g5_requests (effort, case_id, architecture, records_path, manifest_sha256,
                                         request_sha256, quote_usd{column})
                VALUES (?, ?, ?, ?, ?, ?, ?{place})
            """, (*args, previous) if self.recovery else args)
            return cursor.lastrowid, None

    def replaces_number(self, number):
        if self.recovery:
            return self.db.execute("SELECT replaces_number FROM g5_requests WHERE number=?", (number,)).fetchone()[0]
        return None

    def settle(self, number, record, stop_reason):
        metadata = record["response_metadata"] or {}
        details = metadata.get("incomplete_details") or {}
        truncated = int(details.get("reason") == "max_output_tokens")
        cost = record["estimated_cost"]
        with self.db:
            cursor = self.db.execute("""
                UPDATE g5_requests SET cost_usd=?, status=?, error_type=?, truncated=?, record_sha256=?
                WHERE number=? AND status IS NULL AND request_sha256=?
            """, (str(cost) if cost is not None else None, record["status"], record["error_type"],
                  truncated, digest(record), number, record["generation_request_sha256"]))
            require(cursor.rowcount == 1, "G5 reservation could not be reconciled")
            through = self.recovery["through_number"] if self.recovery else 0
            recent = self.db.execute("""
                SELECT status FROM g5_requests WHERE effort = (SELECT effort FROM g5_requests WHERE number=?)
                AND number > ?
                ORDER BY number DESC LIMIT 3
            """, (number, through)).fetchall()
            truncations = self.db.execute("""
                SELECT SUM(truncated) FROM g5_requests
                WHERE effort = (SELECT effort FROM g5_requests WHERE number=?)
            """, (number,)).fetchone()[0]
            if not stop_reason and truncations >= 3:
                stop_reason = "repeated_output_limit"
            if not stop_reason and len(recent) == 3 and all(row["status"] == "error" for row in recent):
                stop_reason = "consecutive_generation_errors"
            self.db.execute("UPDATE g5_requests SET stop_reason=? WHERE number=?", (stop_reason, number))
        return stop_reason


def _amount(value):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid amount in the G5 ledger") from exc
    require(amount.is_finite() and amount >= 0, "Invalid amount in the G5 ledger")
    return amount


def compare_efforts(records):
    groups = {
        effort: {key[1:]: record for key, record in records.items() if key[0] == effort}
        for effort in ("none", "low")
    }
    comparison = {}
    valid = {}
    for effort, group in groups.items():
        rows = list(group.values())
        valid[effort] = {key for key, record in group.items() if record["status"] == "ok"}
        comparison[effort] = {
            "successful_responses": len(valid[effort]), "failed": len(rows) - len(valid[effort]),
            "output_limit_truncations": sum(
                ((row["response_metadata"] or {}).get("incomplete_details") or {}).get("reason") == "max_output_tokens"
                for row in rows
            ),
        }
        for field in ("input_tokens", "output_tokens", "reasoning_tokens", "latency_ms"):
            values = [row[field] for row in rows if row.get(field) is not None]
            comparison[effort][field] = {"observed": len(values), "total": round(sum(values), 6)}
    return {"efforts": comparison, "paired_successful_units": len(valid["none"] & valid["low"]),
            "semantic_status": "NOT_RUN"}
