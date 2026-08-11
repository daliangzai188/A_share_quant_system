"""SQLite事务型执行事件镜像。

该账本只保存执行计划、买入片段和卖出片段的不可变修订历史，不替代当前
``positions.json``和CSV权威账本。相同事件、相同内容重复写入不会新增记录；同一事件
内容变化会追加revision，旧版本永久保留。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping


SCHEMA_VERSION = 1


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    text = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExecutionEventStore:
    """WAL模式、逐事件事务提交的追加式镜像账本。"""

    def __init__(self, path: Path | str):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_uid TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    event_type TEXT NOT NULL,
                    trade_key TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(event_uid, revision),
                    UNIQUE(event_uid, payload_sha256)
                );
                CREATE TABLE IF NOT EXISTS event_heads (
                    event_uid TEXT PRIMARY KEY,
                    sequence_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    FOREIGN KEY(sequence_id) REFERENCES execution_events(sequence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_events_trade
                    ON execution_events(trade_key, event_type);
                CREATE INDEX IF NOT EXISTS idx_execution_events_recorded
                    ON execution_events(recorded_at);
                """
            )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def append_event(
        self,
        *,
        event_uid: str,
        event_type: str,
        trade_key: str,
        payload: Mapping[str, Any],
        recorded_at: str = "",
    ) -> dict[str, Any]:
        uid = str(event_uid or "").strip()
        kind = str(event_type or "").strip().upper()
        if not uid or not kind:
            raise ValueError("event_uid和event_type不能为空")
        payload_json, payload_sha256 = _canonical_payload(payload)
        timestamp = str(recorded_at or "").strip() or dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                head = connection.execute(
                    "SELECT sequence_id, revision, payload_sha256 "
                    "FROM event_heads WHERE event_uid=?",
                    (uid,),
                ).fetchone()
                if head is not None and str(head["payload_sha256"]) == payload_sha256:
                    connection.execute("COMMIT")
                    return {
                        "inserted": False,
                        "restored_existing_revision": False,
                        "sequence_id": int(head["sequence_id"]),
                        "revision": int(head["revision"]),
                        "payload_sha256": payload_sha256,
                    }
                # CSV/JSON权威账本可能在人工纠错、重建或崩溃恢复后回到某个
                # 历史内容。execution_events禁止同一event_uid+payload重复保存，
                # 因此不能再次INSERT；应复用原不可变修订并把head指回它。
                # 这既保留A→B的历史，也允许当前视图合法地从B恢复为A。
                existing = connection.execute(
                    "SELECT sequence_id, revision FROM execution_events "
                    "WHERE event_uid=? AND payload_sha256=?",
                    (uid, payload_sha256),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        "UPDATE event_heads SET sequence_id=?, revision=?, payload_sha256=? "
                        "WHERE event_uid=?",
                        (
                            int(existing["sequence_id"]),
                            int(existing["revision"]),
                            payload_sha256,
                            uid,
                        ),
                    )
                    connection.execute("COMMIT")
                    return {
                        "inserted": False,
                        "restored_existing_revision": True,
                        "sequence_id": int(existing["sequence_id"]),
                        "revision": int(existing["revision"]),
                        "payload_sha256": payload_sha256,
                    }
                revision_row = connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision "
                    "FROM execution_events WHERE event_uid=?",
                    (uid,),
                ).fetchone()
                revision = int(revision_row["next_revision"])
                cursor = connection.execute(
                    "INSERT INTO execution_events("
                    "event_uid, revision, event_type, trade_key, payload_json, "
                    "payload_sha256, recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        uid,
                        revision,
                        kind,
                        str(trade_key or "").strip(),
                        payload_json,
                        payload_sha256,
                        timestamp,
                    ),
                )
                sequence_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO event_heads(event_uid, sequence_id, revision, payload_sha256) "
                    "VALUES(?,?,?,?) ON CONFLICT(event_uid) DO UPDATE SET "
                    "sequence_id=excluded.sequence_id, revision=excluded.revision, "
                    "payload_sha256=excluded.payload_sha256",
                    (uid, sequence_id, revision, payload_sha256),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "inserted": True,
            "restored_existing_revision": False,
            "sequence_id": sequence_id,
            "revision": revision,
            "payload_sha256": payload_sha256,
        }

    def audit(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            event_count = int(
                connection.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
            )
            head_count = int(
                connection.execute("SELECT COUNT(*) FROM event_heads").fetchone()[0]
            )
            broken_heads = int(
                connection.execute(
                    "SELECT COUNT(*) FROM event_heads h LEFT JOIN execution_events e "
                    "ON e.sequence_id=h.sequence_id WHERE e.sequence_id IS NULL"
                ).fetchone()[0]
            )
            counts = {
                str(row["event_type"]): int(row["count"])
                for row in connection.execute(
                    "SELECT e.event_type, COUNT(*) AS count FROM event_heads h "
                    "JOIN execution_events e ON e.sequence_id=h.sequence_id "
                    "GROUP BY e.event_type ORDER BY e.event_type"
                ).fetchall()
            }
            schema_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        return {
            "status": "PASS"
            if integrity == "ok" and broken_heads == 0 and head_count <= event_count
            else "FAIL",
            "path": str(self.path),
            "schema_version": int(schema_row[0]) if schema_row else 0,
            "integrity_check": integrity,
            "event_revision_count": event_count,
            "event_head_count": head_count,
            "broken_head_count": broken_heads,
            "head_count_by_type": counts,
        }

    def event_history(self, event_uid: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence_id, event_uid, revision, event_type, trade_key, "
                "payload_json, payload_sha256, recorded_at FROM execution_events "
                "WHERE event_uid=? ORDER BY revision",
                (str(event_uid),),
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def head_event_uids(self) -> set[str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT event_uid FROM event_heads").fetchall()
        return {str(row[0]) for row in rows}
