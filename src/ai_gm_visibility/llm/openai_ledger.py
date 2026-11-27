"""Persist G4 dispatch reservations across batches and process interruptions."""

import sqlite3
from decimal import Decimal
from pathlib import Path

LEDGER_PATH = Path("runs/g4-openai-budget.sqlite3")
MAX_REQUESTS = 48
MAX_BUDGET_USD = 5


class OpenAILedger:
    def __init__(self, path, request_limit, budget_usd):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.request_limit = request_limit
        self.budget = Decimal(str(budget_usd))
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                request_number INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                repeat_id INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                architecture TEXT NOT NULL,
                quote_usd TEXT NOT NULL,
                estimated_cost_usd TEXT,
                status TEXT,
                safety_stop TEXT,
                UNIQUE (run_id, repeat_id, case_id, architecture)
            )
        """)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.connection.close()

    def _totals(self):
        rows = self.connection.execute(
            "SELECT quote_usd, estimated_cost_usd, status, safety_stop FROM reservations"
        ).fetchall()
        estimated = sum((Decimal(cost) for _, cost, _, _ in rows if cost is not None), Decimal(0))
        reserved = sum((Decimal(quote) for quote, cost, _, _ in rows if cost is None), Decimal(0))
        unsettled = sum(status is None for _, _, status, _ in rows)
        stopped = any(stop is not None for _, _, _, stop in rows)
        return len(rows), estimated, reserved, unsettled, stopped

    def reserve(self, run_id, repeat_id, case_id, architecture, quote):
        # Serialize admission across processes and commit before dispatch.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            used, estimated, reserved, _, stopped = self._totals()
            if stopped:
                return None, "prior_billing_anomaly"
            if used >= self.request_limit:
                return None, "request_limit_reached"
            if estimated + reserved + quote > self.budget:
                return None, "budget_exhausted"
            cursor = self.connection.execute(
                """INSERT INTO reservations (run_id, repeat_id, case_id, architecture, quote_usd)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, repeat_id, case_id, architecture, str(quote)),
            )
            return cursor.lastrowid, None

    def settle(self, number, record, safety_stop):
        cost = record["estimated_cost"]
        with self.connection:
            self.connection.execute(
                """UPDATE reservations SET estimated_cost_usd = ?, status = ?, safety_stop = ?
                   WHERE request_number = ?""",
                (str(cost) if cost is not None else None, record["status"], safety_stop, number),
            )

    def summary(self):
        used, estimated, reserved, unsettled, stopped = self._totals()
        return {
            "request_limit": self.request_limit,
            "requests_used": used,
            "requests_remaining": max(0, self.request_limit - used),
            "budget_usd": float(self.budget),
            "estimated_cost_usd": float(estimated),
            "reserved_cost_usd": float(reserved),
            "unsettled_requests": unsettled,
            "billing_anomaly": stopped,
        }
