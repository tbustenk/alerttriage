"""Stores and retrieves analyst feedback for closed alert analyses."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.logger import get_logger

log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS feedback (
    id             TEXT PRIMARY KEY,
    alert_id       TEXT NOT NULL,
    analysis_id    TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    analyst_id     TEXT NOT NULL,
    analyst_verdict TEXT NOT NULL,
    analyst_notes  TEXT DEFAULT '',
    ai_correct     INTEGER NOT NULL,
    timestamp      TEXT NOT NULL,
    metadata       TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_feedback_client ON feedback(client_id);
CREATE INDEX IF NOT EXISTS idx_feedback_alert  ON feedback(alert_id);
"""


class FeedbackSystem:
    """
    SQLite-backed store for analyst verdicts.

    One database per client, stored under `data/<client_id>/feedback.db`.
    """

    def __init__(self, data_dir: Path, client_id: str) -> None:
        self.client_id = client_id
        db_path = data_dir / client_id / "feedback.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, feedback: FeedbackRecord) -> None:
        """Persist a new feedback record."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO feedback
                  (id, alert_id, analysis_id, client_id, analyst_id,
                   analyst_verdict, analyst_notes, ai_correct, timestamp, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    feedback.id,
                    feedback.alert_id,
                    feedback.analysis_id,
                    feedback.client_id,
                    feedback.analyst_id,
                    feedback.analyst_verdict,
                    feedback.analyst_notes,
                    int(feedback.ai_verdict_was_correct),
                    feedback.timestamp.isoformat(),
                    json.dumps(feedback.metadata),
                ),
            )
        log.info("feedback_recorded", id=feedback.id, correct=feedback.ai_verdict_was_correct)

    def get_for_client(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE client_id=? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (self.client_id, limit, offset),
            ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def accuracy(self) -> float:
        """Return fraction of analyses that matched the analyst verdict."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT AVG(ai_correct) FROM feedback WHERE client_id=?",
                (self.client_id,),
            ).fetchone()
        val = row[0]
        return float(val) if val is not None else 0.0

    def _row_to_model(self, row: sqlite3.Row) -> FeedbackRecord:
        return FeedbackRecord(
            id=row["id"],
            alert_id=row["alert_id"],
            analysis_id=row["analysis_id"],
            client_id=row["client_id"],
            analyst_id=row["analyst_id"],
            analyst_verdict=row["analyst_verdict"],
            analyst_notes=row["analyst_notes"],
            ai_verdict_was_correct=bool(row["ai_correct"]),
            metadata=json.loads(row["metadata"]),
        )
