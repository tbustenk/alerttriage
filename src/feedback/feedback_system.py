"""SQLite-backed store of analyst verdicts.

One database per client at ``data/<client_id>/feedback.db``. The schema
is intentionally narrow — the goal is fast aggregation for
:class:`alerttriage.src.feedback.learning_engine.LearningEngine`, not
forensic search.

Production behaviour:

* The store **batches** writes via :meth:`record_many` to avoid
  per-row transactions when ingesting hundreds of analyst decisions
  from a SOAR export.
* If the on-disk file is **corrupted** (truncated mid-write, FS
  failure), the store archives it next to the original with a
  ``.corrupt-<ts>`` suffix and re-initialises an empty DB so the
  service keeps running. The next sync from upstream rebuilds state.
* ``rule_name`` is promoted to a real column (with an index) instead of
  living inside the JSON metadata blob. The learning engine queries it
  directly, which removes a JSON deserialise per row from the hot path.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alerttriage.src.core.alert_models import FeedbackRecord
from alerttriage.src.logger import get_logger


@dataclass(frozen=True)
class RuleStats:
    """Per-rule counts returned by :meth:`FeedbackSystem.rule_stats`."""

    rule_name: str
    total: int
    correct: int
    false_positives: int
    latest_ts: str | None


log = get_logger(__name__)

_SCHEMA_VERSION = 2

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
    rule_name      TEXT NOT NULL DEFAULT 'unknown',
    timestamp      TEXT NOT NULL,
    metadata       TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_feedback_client    ON feedback(client_id);
CREATE INDEX IF NOT EXISTS idx_feedback_alert     ON feedback(alert_id);
CREATE INDEX IF NOT EXISTS idx_feedback_rule      ON feedback(client_id, rule_name);
CREATE INDEX IF NOT EXISTS idx_feedback_verdict   ON feedback(client_id, analyst_verdict);
CREATE INDEX IF NOT EXISTS idx_feedback_timestamp ON feedback(client_id, timestamp DESC);
"""


class FeedbackStoreError(Exception):
    """Raised when the store cannot be opened or recovered."""


class FeedbackSystem:
    """SQLite-backed store for analyst verdicts (one DB per client)."""

    def __init__(self, data_dir: Path, client_id: str) -> None:
        """Open or create the database for ``client_id``.

        Args:
            data_dir: Root data directory (``data/`` by default).
            client_id: Logical client identifier; used as the directory name.
        """
        self.client_id = client_id
        db_path = data_dir / client_id / "feedback.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, feedback: FeedbackRecord) -> None:
        """Persist a single feedback record (upsert by ``id``)."""
        self.record_many([feedback])

    def record_many(self, feedbacks: Iterable[FeedbackRecord]) -> int:
        """Persist many feedback records in one transaction.

        Returns the number of rows written. Empty input is a no-op (returns 0).
        """
        rows = [self._to_row(f) for f in feedbacks]
        if not rows:
            return 0

        with self._conn() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO feedback
                  (id, alert_id, analysis_id, client_id, analyst_id,
                   analyst_verdict, analyst_notes, ai_correct,
                   rule_name, timestamp, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        log.info("feedback_recorded_batch", client=self.client_id, count=len(rows))
        return len(rows)

    def get_for_client(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        """Return up to ``limit`` records, newest first, after skipping ``offset``."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE client_id=? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (self.client_id, limit, offset),
            ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def accuracy(self) -> float:
        """Fraction of analyses that matched the analyst verdict (0.0 if empty)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT AVG(ai_correct) FROM feedback WHERE client_id=?",
                (self.client_id,),
            ).fetchone()
        val = row[0]
        return float(val) if val is not None else 0.0

    def rule_stats(self) -> list[RuleStats]:
        """Return per-rule counts without deserialising any JSON.

        Used by :class:`LearningEngine` so the hot path never touches JSON.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    rule_name,
                    COUNT(*)                                          AS total,
                    SUM(ai_correct)                                   AS correct,
                    SUM(CASE WHEN analyst_verdict='false_positive'
                             THEN 1 ELSE 0 END)                       AS false_positives,
                    MAX(timestamp)                                    AS latest_ts
                FROM feedback
                WHERE client_id = ?
                GROUP BY rule_name
                """,
                (self.client_id,),
            ).fetchall()
        return [
            RuleStats(
                rule_name=str(r["rule_name"]),
                total=int(r["total"]),
                correct=int(r["correct"] or 0),
                false_positives=int(r["false_positives"] or 0),
                latest_ts=r["latest_ts"],
            )
            for r in rows
        ]

    def recent_misclassifications(self, rule_name: str, *, limit: int = 5) -> list[str]:
        """Return the most recent analyst verdicts where the AI was wrong."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT analyst_verdict FROM feedback "
                "WHERE client_id=? AND rule_name=? AND ai_correct=0 "
                "ORDER BY timestamp DESC LIMIT ?",
                (self.client_id, rule_name, limit),
            ).fetchall()
        return [r["analyst_verdict"] for r in rows]

    # ------------------------------------------------------------------
    # Internal — connection management & recovery
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            with self._conn() as conn:
                conn.executescript(_DDL)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            self._recover_corrupt_db(exc)
            with self._conn() as conn:
                conn.executescript(_DDL)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _recover_corrupt_db(self, exc: sqlite3.DatabaseError) -> None:
        """Move a corrupt DB aside so a fresh one can be created.

        We don't try to salvage rows — they are recoverable from the upstream
        SOAR/SIEM. Service continuity matters more than the few hours of
        feedback the corrupt file may contain.
        """
        backup = self._db_path.with_suffix(f".corrupt-{int(time.time())}")
        log.error(
            "feedback_db_corrupt",
            client=self.client_id,
            db_path=str(self._db_path),
            backup_path=str(backup),
            error=str(exc),
        )
        try:
            shutil.move(str(self._db_path), str(backup))
        except OSError as move_exc:
            raise FeedbackStoreError(
                f"feedback DB at {self._db_path} is corrupt and could not be archived"
            ) from move_exc

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Row <-> model
    # ------------------------------------------------------------------

    @staticmethod
    def _to_row(feedback: FeedbackRecord) -> tuple[object, ...]:
        return (
            feedback.id,
            feedback.alert_id,
            feedback.analysis_id,
            feedback.client_id,
            feedback.analyst_id,
            feedback.analyst_verdict,
            feedback.analyst_notes,
            int(feedback.ai_verdict_was_correct),
            feedback.metadata.get("rule_name", "unknown"),
            feedback.timestamp.isoformat(),
            json.dumps(feedback.metadata),
        )

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> FeedbackRecord:
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            log.warning("feedback_metadata_corrupt", id=row["id"])
            metadata = {}
        if "rule_name" not in metadata:
            metadata["rule_name"] = row["rule_name"]
        return FeedbackRecord(
            id=row["id"],
            alert_id=row["alert_id"],
            analysis_id=row["analysis_id"],
            client_id=row["client_id"],
            analyst_id=row["analyst_id"],
            analyst_verdict=row["analyst_verdict"],
            analyst_notes=row["analyst_notes"] or "",
            ai_verdict_was_correct=bool(row["ai_correct"]),
            metadata=metadata,
        )
