from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextRecord:
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    payload_hash: str
    delivered_at: str
    stored_at: str


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    conversation_id: str
    merchant_id: str | None
    customer_id: str | None
    trigger_id: str | None
    state: str
    auto_reply_count: int
    last_body: str | None
    context: dict[str, Any]


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS contexts (
            scope TEXT NOT NULL,
            context_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            PRIMARY KEY (scope, context_id)
        );
        CREATE TABLE IF NOT EXISTS suppressions (
            suppression_key TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            trigger_id TEXT NOT NULL,
            conversation_id TEXT,
            status TEXT NOT NULL,
            reserved_at TEXT NOT NULL,
            sent_at TEXT,
            expires_at TEXT,
            PRIMARY KEY (suppression_key, recipient_id)
        );
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            merchant_id TEXT,
            customer_id TEXT,
            trigger_id TEXT,
            state TEXT NOT NULL,
            auto_reply_count INTEGER NOT NULL DEFAULT 0,
            last_body TEXT,
            context_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS turns (
            conversation_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            from_role TEXT NOT NULL,
            message TEXT NOT NULL,
            received_at TEXT NOT NULL,
            PRIMARY KEY (conversation_id, turn_number, from_role)
        );
        CREATE TABLE IF NOT EXISTS merchant_fingerprints (
            merchant_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            seen_count INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (merchant_id, fingerprint)
        );
        CREATE TABLE IF NOT EXISTS generations (
            cache_key TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            selected_fact_ids_json TEXT NOT NULL,
            composer TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cooldowns (
            recipient_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            until_at TEXT,
            permanent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(schema)

    def put_context(
        self,
        *,
        scope: str,
        context_id: str,
        version: int,
        payload: dict[str, Any],
        delivered_at: str,
    ) -> tuple[bool, ContextRecord | None]:
        payload_json = canonical_json(payload)
        payload_hash = stable_hash(payload)
        stored_at = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM contexts WHERE scope = ? AND context_id = ?",
                (scope, context_id),
            ).fetchone()
            if current is not None and int(current["version"]) >= version:
                connection.rollback()
                return False, self._context_from_row(current)
            connection.execute(
                """
                INSERT INTO contexts
                    (scope, context_id, version, payload_json, payload_hash,
                     delivered_at, stored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, context_id) DO UPDATE SET
                    version = excluded.version,
                    payload_json = excluded.payload_json,
                    payload_hash = excluded.payload_hash,
                    delivered_at = excluded.delivered_at,
                    stored_at = excluded.stored_at
                """,
                (
                    scope,
                    context_id,
                    version,
                    payload_json,
                    payload_hash,
                    delivered_at,
                    stored_at,
                ),
            )
            connection.commit()
        return True, ContextRecord(
            scope=scope,
            context_id=context_id,
            version=version,
            payload=payload,
            payload_hash=payload_hash,
            delivered_at=delivered_at,
            stored_at=stored_at,
        )

    @staticmethod
    def _context_from_row(row: sqlite3.Row) -> ContextRecord:
        return ContextRecord(
            scope=str(row["scope"]),
            context_id=str(row["context_id"]),
            version=int(row["version"]),
            payload=json.loads(row["payload_json"]),
            payload_hash=str(row["payload_hash"]),
            delivered_at=str(row["delivered_at"]),
            stored_at=str(row["stored_at"]),
        )

    def get_context(self, scope: str, context_id: str) -> ContextRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM contexts WHERE scope = ? AND context_id = ?",
                (scope, context_id),
            ).fetchone()
        return self._context_from_row(row) if row is not None else None

    def context_counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT scope, COUNT(*) AS n FROM contexts GROUP BY scope"
            ).fetchall()
        for row in rows:
            counts[str(row["scope"])] = int(row["n"])
        return counts

    def reserve_suppression(
        self,
        *,
        suppression_key: str,
        recipient_id: str,
        trigger_id: str,
        expires_at: str | None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO suppressions
                        (suppression_key, recipient_id, trigger_id, status,
                         reserved_at, expires_at)
                    VALUES (?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        suppression_key,
                        recipient_id,
                        trigger_id,
                        utc_now(),
                        expires_at,
                    ),
                )
                connection.commit()
                return True
            except sqlite3.IntegrityError:
                connection.rollback()
                return False

    def mark_suppression_sent(
        self,
        suppression_key: str,
        recipient_id: str,
        conversation_id: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE suppressions
                   SET status = 'sent', conversation_id = ?, sent_at = ?
                 WHERE suppression_key = ? AND recipient_id = ?
                """,
                (conversation_id, utc_now(), suppression_key, recipient_id),
            )

    def release_suppression(self, suppression_key: str, recipient_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM suppressions WHERE suppression_key = ? AND recipient_id = ? AND status = 'reserved'",
                (suppression_key, recipient_id),
            )

    def create_conversation(
        self,
        *,
        conversation_id: str,
        merchant_id: str | None,
        customer_id: str | None,
        trigger_id: str | None,
        state: str,
        last_body: str | None,
        context: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations
                    (conversation_id, merchant_id, customer_id, trigger_id,
                     state, last_body, context_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    merchant_id = COALESCE(excluded.merchant_id, conversations.merchant_id),
                    customer_id = COALESCE(excluded.customer_id, conversations.customer_id),
                    trigger_id = COALESCE(excluded.trigger_id, conversations.trigger_id),
                    state = excluded.state,
                    last_body = COALESCE(excluded.last_body, conversations.last_body),
                    context_json = excluded.context_json,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    merchant_id,
                    customer_id,
                    trigger_id,
                    state,
                    last_body,
                    canonical_json(context),
                    now,
                    now,
                ),
            )

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            merchant_id=row["merchant_id"],
            customer_id=row["customer_id"],
            trigger_id=row["trigger_id"],
            state=str(row["state"]),
            auto_reply_count=int(row["auto_reply_count"]),
            last_body=row["last_body"],
            context=json.loads(row["context_json"]),
        )

    def update_conversation(
        self,
        conversation_id: str,
        *,
        state: str,
        last_body: str | None = None,
        auto_reply_count: int | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE conversations SET
                    state = ?,
                    last_body = COALESCE(?, last_body),
                    auto_reply_count = COALESCE(?, auto_reply_count),
                    updated_at = ?
                WHERE conversation_id = ?
                """,
                (state, last_body, auto_reply_count, utc_now(), conversation_id),
            )

    def append_turn(
        self,
        *,
        conversation_id: str,
        turn_number: int,
        from_role: str,
        message: str,
        received_at: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO turns
                    (conversation_id, turn_number, from_role, message, received_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    turn_number,
                    from_role,
                    message,
                    received_at or utc_now(),
                ),
            )

    def record_fingerprint(self, merchant_id: str, fingerprint: str) -> int:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT seen_count FROM merchant_fingerprints WHERE merchant_id = ? AND fingerprint = ?",
                (merchant_id, fingerprint),
            ).fetchone()
            count = (int(row["seen_count"]) if row else 0) + 1
            connection.execute(
                """
                INSERT INTO merchant_fingerprints
                    (merchant_id, fingerprint, seen_count, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(merchant_id, fingerprint) DO UPDATE SET
                    seen_count = excluded.seen_count,
                    last_seen_at = excluded.last_seen_at
                """,
                (merchant_id, fingerprint, count, utc_now()),
            )
            connection.commit()
        return count

    def set_cooldown(
        self,
        recipient_id: str,
        *,
        reason: str,
        until_at: str | None = None,
        permanent: bool = False,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cooldowns
                    (recipient_id, reason, until_at, permanent, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recipient_id) DO UPDATE SET
                    reason = excluded.reason,
                    until_at = excluded.until_at,
                    permanent = excluded.permanent,
                    created_at = excluded.created_at
                """,
                (recipient_id, reason, until_at, int(permanent), utc_now()),
            )

    def is_cooled_down(self, recipient_id: str, now_iso: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT permanent, until_at FROM cooldowns WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()
        if row is None:
            return False
        if int(row["permanent"]):
            return True
        until = row["until_at"]
        return bool(until and str(until) > now_iso)

    def get_generation(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response_json FROM generations WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        return json.loads(row["response_json"]) if row is not None else None

    def save_generation(
        self,
        *,
        cache_key: str,
        response: dict[str, Any],
        selected_fact_ids: list[str],
        composer: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO generations
                    (cache_key, response_json, selected_fact_ids_json,
                     composer, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    canonical_json(response),
                    canonical_json(selected_fact_ids),
                    composer,
                    utc_now(),
                ),
            )
