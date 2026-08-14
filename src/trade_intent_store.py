"""统一交易意图与执行状态事务账本。

本模块与 :mod:`src.execution_event_store` 共用同一个 SQLite 文件，但职责不同：

* ``execution_events`` 保留历史成交审计的不可变镜像；
* ``trade_intents`` 是新架构中下单、撤单、成交确认和重启恢复的权威状态；
* ``trade_intent_transitions`` 永久保存每次状态迁移；
* ``trade_recovery_runs`` 记录每次以券商真实状态为依据的恢复批次。

状态更新使用 ``BEGIN IMMEDIATE``、版本号比较和单调成交量，防止多线程、重试或
QMT 滞后快照把已确认状态回退。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


TRADE_INTENT_SCHEMA_VERSION = 1

STATUS_PLANNED = "PLANNED"
STATUS_VALIDATED = "VALIDATED"
STATUS_PREPARED = "PREPARED"
STATUS_SUBMITTING = "SUBMITTING"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
STATUS_CANCEL_REQUESTED = "CANCEL_REQUESTED"
STATUS_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
STATUS_FILLED = "FILLED"
STATUS_CANCELLED = "CANCELLED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"

TERMINAL_STATUSES = frozenset(
    {STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED, STATUS_FAILED}
)
RECOVERABLE_STATUSES = frozenset(
    {
        STATUS_PREPARED,
        STATUS_SUBMITTING,
        STATUS_SUBMITTED,
        STATUS_PARTIALLY_FILLED,
        STATUS_CANCEL_REQUESTED,
        STATUS_RECOVERY_REQUIRED,
    }
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PLANNED: frozenset({STATUS_VALIDATED, STATUS_REJECTED, STATUS_FAILED}),
    STATUS_VALIDATED: frozenset({STATUS_PREPARED, STATUS_REJECTED, STATUS_FAILED}),
    STATUS_PREPARED: frozenset(
        {STATUS_SUBMITTING, STATUS_REJECTED, STATUS_FAILED, STATUS_RECOVERY_REQUIRED}
    ),
    STATUS_SUBMITTING: frozenset(
        {STATUS_SUBMITTED, STATUS_REJECTED, STATUS_FAILED, STATUS_RECOVERY_REQUIRED}
    ),
    STATUS_SUBMITTED: frozenset(
        {
            STATUS_PARTIALLY_FILLED,
            STATUS_FILLED,
            STATUS_CANCEL_REQUESTED,
            STATUS_CANCELLED,
            STATUS_REJECTED,
            STATUS_RECOVERY_REQUIRED,
        }
    ),
    STATUS_PARTIALLY_FILLED: frozenset(
        {
            STATUS_FILLED,
            STATUS_CANCEL_REQUESTED,
            STATUS_CANCELLED,
            STATUS_RECOVERY_REQUIRED,
        }
    ),
    STATUS_CANCEL_REQUESTED: frozenset(
        {
            STATUS_PARTIALLY_FILLED,
            STATUS_FILLED,
            STATUS_CANCELLED,
            STATUS_RECOVERY_REQUIRED,
        }
    ),
    STATUS_RECOVERY_REQUIRED: frozenset(
        {
            STATUS_SUBMITTED,
            STATUS_PARTIALLY_FILLED,
            STATUS_FILLED,
            STATUS_CANCEL_REQUESTED,
            STATUS_CANCELLED,
            STATUS_REJECTED,
            STATUS_FAILED,
        }
    ),
    STATUS_FILLED: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_REJECTED: frozenset(),
    STATUS_FAILED: frozenset(),
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(
        dict(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def build_idempotency_key(
    *,
    account_fingerprint: str,
    business_date: str,
    strategy_leg: str,
    side: str,
    ts_code: str,
    purpose: str,
    source_key: str,
) -> str:
    """生成跨进程稳定的业务幂等键，不包含账号明文。"""

    parts = (
        str(account_fingerprint or "").strip(),
        str(business_date or "").strip(),
        str(strategy_leg or "").strip().upper(),
        str(side or "").strip().upper(),
        str(ts_code or "").strip().upper(),
        str(purpose or "").strip().upper(),
        str(source_key or "").strip(),
    )
    if not all(parts):
        raise ValueError("生成交易意图幂等键所需字段不完整")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TradeIntentSpec:
    """策略层产生的不可变交易意图。"""

    idempotency_key: str
    account_fingerprint: str
    strategy_leg: str
    side: str
    ts_code: str
    business_date: str
    purpose: str
    source_key: str
    target_qty: int
    target_amount: float = 0.0
    price_type: str = "FIXED_PRICE"
    limit_price: float = 0.0
    signal_date: str = ""
    planned_exit_date: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    intent_id: str = ""

    def normalized(self) -> "TradeIntentSpec":
        side = str(self.side or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"不支持的交易方向:{self.side}")
        quantity = int(self.target_qty or 0)
        if quantity <= 0:
            raise ValueError("交易意图target_qty必须大于0")
        idempotency_key = str(self.idempotency_key or "").strip()
        if not idempotency_key:
            raise ValueError("交易意图idempotency_key不能为空")
        return TradeIntentSpec(
            idempotency_key=idempotency_key,
            account_fingerprint=str(self.account_fingerprint or "").strip(),
            strategy_leg=str(self.strategy_leg or "").strip().upper(),
            side=side,
            ts_code=str(self.ts_code or "").strip().upper(),
            business_date=str(self.business_date or "").strip(),
            purpose=str(self.purpose or "").strip().upper(),
            source_key=str(self.source_key or "").strip(),
            target_qty=quantity,
            target_amount=max(float(self.target_amount or 0.0), 0.0),
            price_type=str(self.price_type or "").strip().upper(),
            limit_price=max(float(self.limit_price or 0.0), 0.0),
            signal_date=str(self.signal_date or "").strip(),
            planned_exit_date=str(self.planned_exit_date or "").strip(),
            metadata=dict(self.metadata or {}),
            intent_id=str(self.intent_id or "").strip() or uuid.uuid4().hex,
        )


class TradeIntentStore:
    """SQLite WAL事务账本；同一数据库文件可安全供多个线程/进程访问。"""

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
                CREATE TABLE IF NOT EXISTS trade_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_intents (
                    intent_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    account_fingerprint TEXT NOT NULL,
                    strategy_leg TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    ts_code TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    signal_date TEXT NOT NULL DEFAULT '',
                    planned_exit_date TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    target_qty INTEGER NOT NULL CHECK(target_qty > 0),
                    target_amount REAL NOT NULL DEFAULT 0 CHECK(target_amount >= 0),
                    price_type TEXT NOT NULL,
                    limit_price REAL NOT NULL DEFAULT 0 CHECK(limit_price >= 0),
                    status TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL DEFAULT '',
                    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK(filled_qty >= 0),
                    filled_amount REAL NOT NULL DEFAULT 0 CHECK(filled_amount >= 0),
                    avg_fill_price REAL NOT NULL DEFAULT 0 CHECK(avg_fill_price >= 0),
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_intents_broker_order
                    ON trade_intents(broker_order_id) WHERE broker_order_id <> '';
                CREATE INDEX IF NOT EXISTS idx_trade_intents_recovery
                    ON trade_intents(status, business_date);
                CREATE INDEX IF NOT EXISTS idx_trade_intents_symbol
                    ON trade_intents(ts_code, side, business_date);
                CREATE TABLE IF NOT EXISTS trade_intent_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES trade_intents(intent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_trade_transitions_intent
                    ON trade_intent_transitions(intent_id, transition_id);
                CREATE TABLE IF NOT EXISTS trade_recovery_runs (
                    recovery_id TEXT PRIMARY KEY,
                    daemon_boot_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    broker_snapshot_sha256 TEXT NOT NULL DEFAULT '',
                    recovered_count INTEGER NOT NULL DEFAULT 0,
                    unresolved_count INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            connection.execute(
                "INSERT INTO trade_schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(TRADE_INTENT_SCHEMA_VERSION),),
            )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["metadata"] = json.loads(str(payload.pop("metadata_json", "{}") or "{}"))
        return payload

    @staticmethod
    def _immutable_payload(spec: TradeIntentSpec) -> dict[str, Any]:
        payload = asdict(spec)
        payload["metadata"] = dict(spec.metadata or {})
        return payload

    def create_intent(self, spec: TradeIntentSpec) -> dict[str, Any]:
        """幂等创建PLANNED意图；同一幂等键但业务字段不同会硬失败。"""

        normalized = spec.normalized()
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM trade_intents WHERE idempotency_key=?",
                    (normalized.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    current = self._row_to_dict(existing) or {}
                    expected = self._immutable_payload(normalized)
                    immutable_columns = (
                        "account_fingerprint",
                        "strategy_leg",
                        "side",
                        "ts_code",
                        "business_date",
                        "signal_date",
                        "planned_exit_date",
                        "purpose",
                        "source_key",
                        "target_qty",
                        "target_amount",
                        "price_type",
                        "limit_price",
                        "metadata",
                    )
                    mismatch = [
                        name for name in immutable_columns if current.get(name) != expected.get(name)
                    ]
                    if mismatch:
                        raise ValueError(
                            "同一idempotency_key对应不同交易意图:" + ",".join(mismatch)
                        )
                    connection.execute("COMMIT")
                    current["created"] = False
                    return current

                connection.execute(
                    "INSERT INTO trade_intents("
                    "intent_id,idempotency_key,account_fingerprint,strategy_leg,side,"
                    "ts_code,business_date,signal_date,planned_exit_date,purpose,source_key,"
                    "target_qty,target_amount,price_type,limit_price,status,metadata_json,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        normalized.intent_id,
                        normalized.idempotency_key,
                        normalized.account_fingerprint,
                        normalized.strategy_leg,
                        normalized.side,
                        normalized.ts_code,
                        normalized.business_date,
                        normalized.signal_date,
                        normalized.planned_exit_date,
                        normalized.purpose,
                        normalized.source_key,
                        normalized.target_qty,
                        normalized.target_amount,
                        normalized.price_type,
                        normalized.limit_price,
                        STATUS_PLANNED,
                        _canonical_json(normalized.metadata),
                        now,
                        now,
                    ),
                )
                snapshot = connection.execute(
                    "SELECT * FROM trade_intents WHERE intent_id=?", (normalized.intent_id,)
                ).fetchone()
                connection.execute(
                    "INSERT INTO trade_intent_transitions("
                    "intent_id,from_status,to_status,from_version,to_version,reason,"
                    "snapshot_json,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        normalized.intent_id,
                        "",
                        STATUS_PLANNED,
                        0,
                        1,
                        "策略产生交易意图",
                        _canonical_json(dict(snapshot) if snapshot else {}),
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        created = self.get_intent(normalized.intent_id)
        if created is None:
            raise RuntimeError("交易意图创建后无法读取")
        created["created"] = True
        return created

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM trade_intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
        return self._row_to_dict(row)

    def get_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM trade_intents WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
        return self._row_to_dict(row)

    def transition_intent(
        self,
        intent_id: str,
        to_status: str,
        *,
        expected_statuses: Iterable[str] | None = None,
        expected_version: int | None = None,
        reason: str = "",
        broker_order_id: str | None = None,
        filled_qty: int | None = None,
        filled_amount: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """以CAS方式迁移状态；成交数量/金额只允许单调增加。"""

        target_status = str(to_status or "").strip().upper()
        if target_status not in _ALLOWED_TRANSITIONS:
            raise ValueError(f"未知交易意图状态:{to_status}")
        expected = (
            {str(item).strip().upper() for item in expected_statuses}
            if expected_statuses is not None
            else None
        )
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM trade_intents WHERE intent_id=?", (str(intent_id),)
                ).fetchone()
                if row is None:
                    raise KeyError(f"交易意图不存在:{intent_id}")
                current = self._row_to_dict(row) or {}
                old_status = str(current["status"])
                old_version = int(current["version"])
                if expected is not None and old_status not in expected:
                    raise RuntimeError(
                        f"交易意图状态CAS失败:{intent_id} current={old_status} expected={sorted(expected)}"
                    )
                if expected_version is not None and old_version != int(expected_version):
                    raise RuntimeError(
                        f"交易意图版本CAS失败:{intent_id} current={old_version} expected={expected_version}"
                    )
                if target_status != old_status and target_status not in _ALLOWED_TRANSITIONS[old_status]:
                    raise RuntimeError(f"非法交易意图迁移:{old_status}->{target_status}")

                old_oid = str(current.get("broker_order_id", "") or "")
                next_oid = old_oid if broker_order_id is None else str(broker_order_id or "")
                if old_oid and next_oid and old_oid != next_oid:
                    raise RuntimeError(f"交易意图券商单号冲突:{old_oid}!={next_oid}")
                old_qty = int(current.get("filled_qty", 0) or 0)
                next_qty = old_qty if filled_qty is None else max(old_qty, int(filled_qty or 0))
                target_qty = int(current["target_qty"])
                if next_qty > target_qty:
                    raise RuntimeError(f"成交数量超过交易意图目标:{next_qty}>{target_qty}")
                old_amount = float(current.get("filled_amount", 0.0) or 0.0)
                next_amount = (
                    old_amount
                    if filled_amount is None
                    else max(old_amount, float(filled_amount or 0.0))
                )
                next_metadata = dict(current.get("metadata") or {})
                next_metadata.update(dict(metadata_patch or {}))
                next_version = old_version + 1
                avg_fill_price = next_amount / next_qty if next_qty > 0 else 0.0
                connection.execute(
                    "UPDATE trade_intents SET status=?,broker_order_id=?,filled_qty=?,"
                    "filled_amount=?,avg_fill_price=?,error_code=?,error_message=?,"
                    "metadata_json=?,version=?,updated_at=? WHERE intent_id=? AND version=?",
                    (
                        target_status,
                        next_oid,
                        next_qty,
                        next_amount,
                        avg_fill_price,
                        str(current.get("error_code", "") if error_code is None else error_code),
                        str(
                            current.get("error_message", "")
                            if error_message is None
                            else error_message
                        )[:1000],
                        _canonical_json(next_metadata),
                        next_version,
                        now,
                        str(intent_id),
                        old_version,
                    ),
                )
                updated_row = connection.execute(
                    "SELECT * FROM trade_intents WHERE intent_id=?", (str(intent_id),)
                ).fetchone()
                if updated_row is None:
                    raise RuntimeError("交易意图迁移后无法读取")
                connection.execute(
                    "INSERT INTO trade_intent_transitions("
                    "intent_id,from_status,to_status,from_version,to_version,reason,"
                    "snapshot_json,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(intent_id),
                        old_status,
                        target_status,
                        old_version,
                        next_version,
                        str(reason or ""),
                        _canonical_json(dict(updated_row)),
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        result = self.get_intent(str(intent_id))
        if result is None:
            raise RuntimeError("交易意图迁移后无法读取")
        return result

    def list_recoverable_intents(
        self,
        *,
        account_fingerprint: str = "",
        business_date_on_or_before: str = "",
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in RECOVERABLE_STATUSES)
        clauses = [f"status IN ({placeholders})"]
        params: list[Any] = sorted(RECOVERABLE_STATUSES)
        if account_fingerprint:
            clauses.append("account_fingerprint=?")
            params.append(str(account_fingerprint))
        if business_date_on_or_before:
            clauses.append("business_date<=?")
            params.append(str(business_date_on_or_before))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM trade_intents WHERE " + " AND ".join(clauses)
                + " ORDER BY business_date,intent_id",
                tuple(params),
            ).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    def start_recovery_run(self, daemon_boot_id: str) -> str:
        recovery_id = uuid.uuid4().hex
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO trade_recovery_runs("
                "recovery_id,daemon_boot_id,started_at,status) VALUES(?,?,?,?)",
                (recovery_id, str(daemon_boot_id), _utc_now(), "RUNNING"),
            )
        return recovery_id

    def finish_recovery_run(
        self,
        recovery_id: str,
        *,
        status: str,
        broker_snapshot_sha256: str,
        recovered_count: int,
        unresolved_count: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE trade_recovery_runs SET completed_at=?,status=?,"
                "broker_snapshot_sha256=?,recovered_count=?,unresolved_count=?,"
                "details_json=? WHERE recovery_id=?",
                (
                    _utc_now(),
                    str(status or "").upper(),
                    str(broker_snapshot_sha256 or ""),
                    max(int(recovered_count or 0), 0),
                    max(int(unresolved_count or 0), 0),
                    _canonical_json(details),
                    str(recovery_id),
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"恢复批次不存在:{recovery_id}")

    def transition_history(self, intent_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM trade_intent_transitions WHERE intent_id=? "
                "ORDER BY transition_id",
                (str(intent_id),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(str(item.pop("snapshot_json", "{}") or "{}"))
            result.append(item)
        return result

    def audit(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            intent_count = int(connection.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0])
            transition_count = int(
                connection.execute("SELECT COUNT(*) FROM trade_intent_transitions").fetchone()[0]
            )
            orphan_transitions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM trade_intent_transitions t LEFT JOIN trade_intents i "
                    "ON i.intent_id=t.intent_id WHERE i.intent_id IS NULL"
                ).fetchone()[0]
            )
            invalid_fills = int(
                connection.execute(
                    "SELECT COUNT(*) FROM trade_intents WHERE filled_qty>target_qty"
                ).fetchone()[0]
            )
            counts = {
                str(row["status"]): int(row["n"])
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS n FROM trade_intents GROUP BY status ORDER BY status"
                ).fetchall()
            }
            schema_row = connection.execute(
                "SELECT value FROM trade_schema_meta WHERE key='schema_version'"
            ).fetchone()
        passed = integrity == "ok" and orphan_transitions == 0 and invalid_fills == 0
        return {
            "status": "PASS" if passed else "FAIL",
            "path": str(self.path),
            "schema_version": int(schema_row[0]) if schema_row else 0,
            "integrity_check": integrity,
            "intent_count": intent_count,
            "transition_count": transition_count,
            "orphan_transition_count": orphan_transitions,
            "invalid_fill_count": invalid_fills,
            "intent_count_by_status": counts,
        }
