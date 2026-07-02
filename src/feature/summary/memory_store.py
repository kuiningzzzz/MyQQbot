from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


MEMORY_KINDS = {
    "user_fact",
    "topic",
    "instruction",
    "reminder",
    "group_fact",
    "explicit",
}
MEMORY_STATUSES = {"active", "conflicted", "forgotten", "expired"}
SCOPE_TYPES = {"global", "group", "private"}


@dataclass(frozen=True)
class MemoryCandidate:
    kind: str
    content: str
    normalized_key: str
    subject_user_id: int | None = None
    confidence: float = 0.8
    importance: float = 0.6
    expires_days: int | None = None


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    kind: str
    content: str
    normalized_key: str
    subject_user_id: int | None
    scope_type: str
    scope_id: str | None
    source_conversation: str
    source_excerpt: str
    confidence: float
    importance: float
    status: str
    explicit: bool
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    access_count: int
    expires_at: str | None
    supersedes_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._initialize()

    def upsert_candidate(
        self,
        candidate: MemoryCandidate,
        scope_type: str,
        scope_id: str | None,
        source_conversation: str,
        source_excerpt: str,
        *,
        explicit: bool = False,
    ) -> MemoryRecord:
        value = self._normalize_candidate(candidate)
        scope_type, scope_id = self._normalize_scope(scope_type, scope_id)
        now = _utc_now()
        expires_at = (
            (datetime.now(UTC) + timedelta(days=value.expires_days)).isoformat()
            if value.expires_days is not None
            else None
        )
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                """
                SELECT * FROM memories
                WHERE normalized_key = ? AND kind = ?
                  AND subject_user_id IS ? AND scope_type = ? AND scope_id IS ?
                  AND status IN ('active', 'conflicted')
                ORDER BY id DESC LIMIT 1
                """,
                (
                    value.normalized_key,
                    value.kind,
                    value.subject_user_id,
                    scope_type,
                    scope_id,
                ),
            ).fetchone()
            if existing is not None and _content_key(existing["content"]) == _content_key(
                value.content
            ):
                confidence = 1 - (1 - float(existing["confidence"])) * (
                    1 - value.confidence
                )
                connection.execute(
                    """
                    UPDATE memories
                    SET confidence = ?, importance = MAX(importance, ?),
                        status = 'active', updated_at = ?, expires_at = COALESCE(?, expires_at),
                        source_conversation = ?, source_excerpt = ?, explicit = MAX(explicit, ?)
                    WHERE id = ?
                    """,
                    (
                        confidence,
                        value.importance,
                        now,
                        expires_at,
                        source_conversation,
                        source_excerpt,
                        int(explicit),
                        existing["id"],
                    ),
                )
                memory_id = int(existing["id"])
            else:
                supersedes_id = int(existing["id"]) if existing is not None else None
                if existing is not None:
                    connection.execute(
                        "UPDATE memories SET status = 'conflicted', updated_at = ? WHERE id = ?",
                        (now, existing["id"]),
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO memories (
                        kind, content, normalized_key, subject_user_id,
                        scope_type, scope_id, source_conversation, source_excerpt,
                        confidence, importance, status, explicit,
                        created_at, updated_at, expires_at, supersedes_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (
                        value.kind,
                        value.content,
                        value.normalized_key,
                        value.subject_user_id,
                        scope_type,
                        scope_id,
                        source_conversation,
                        source_excerpt,
                        value.confidence,
                        value.importance,
                        int(explicit),
                        now,
                        now,
                        expires_at,
                        supersedes_id,
                    ),
                )
                memory_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO memory_sources (memory_id, conversation, excerpt, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, source_conversation, source_excerpt[:1000], now),
            )
            connection.commit()
            return self.get(memory_id, connection=connection)

    def retrieve(
        self,
        scope_type: str,
        scope_id: str | None,
        query: str,
        subject_user_id: int | None,
        limit: int,
        min_confidence: float,
    ) -> list[MemoryRecord]:
        scope_type, scope_id = self._normalize_scope(scope_type, scope_id)
        self.expire_due()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE status = 'active' AND confidence >= ?
                  AND (scope_type = 'global' OR (scope_type = ? AND scope_id IS ?))
                ORDER BY importance DESC, updated_at DESC
                LIMIT 5000
                """,
                (min_confidence, scope_type, scope_id),
            ).fetchall()
            query_terms = _terms(query)
            now = datetime.now(UTC)
            scored: list[tuple[float, sqlite3.Row]] = []
            for row in rows:
                lexical = _overlap(query_terms, _terms(row["content"]))
                subject_bonus = (
                    0.22
                    if subject_user_id is not None
                    and row["subject_user_id"] == subject_user_id
                    else 0.0
                )
                updated = _parse_time(row["updated_at"])
                age_days = max(0.0, (now - updated).total_seconds() / 86400)
                recency = math.exp(-age_days / 90)
                access = min(1.0, math.log1p(int(row["access_count"])) / 4)
                score = (
                    lexical * 0.34
                    + float(row["importance"]) * 0.25
                    + float(row["confidence"]) * 0.2
                    + recency * 0.11
                    + access * 0.1
                    + subject_bonus
                )
                if lexical > 0 or subject_bonus > 0 or row["explicit"]:
                    scored.append((score, row))
            selected = [row for _, row in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]]
            if selected:
                now_text = _utc_now()
                connection.executemany(
                    """
                    UPDATE memories SET access_count = access_count + 1,
                        last_accessed_at = ? WHERE id = ?
                    """,
                    [(now_text, row["id"]) for row in selected],
                )
                connection.commit()
            return [self._row_to_record(row) for row in selected]

    def list(
        self,
        query: str = "",
        status: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        self.expire_due()
        clauses: list[str] = []
        params: list[Any] = []
        if status and status in MEMORY_STATUSES:
            clauses.append("status = ?")
            params.append(status)
        if query.strip():
            clauses.append("(content LIKE ? OR normalized_key LIKE ? OR source_excerpt LIKE ?)")
            pattern = f"%{query.strip()}%"
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(500, limit)))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM memories {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def get(
        self,
        memory_id: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> MemoryRecord:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            return self._row_to_record(row)
        with self._lock, closing(self._connect()) as owned:
            row = owned.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise KeyError(memory_id)
            return self._row_to_record(row)

    def update(self, memory_id: int, **changes: Any) -> MemoryRecord:
        allowed = {
            "kind",
            "content",
            "normalized_key",
            "subject_user_id",
            "scope_type",
            "scope_id",
            "confidence",
            "importance",
            "status",
            "expires_at",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return self.get(memory_id)
        if "kind" in values and values["kind"] not in MEMORY_KINDS:
            raise ValueError("invalid memory kind")
        if "status" in values and values["status"] not in MEMORY_STATUSES:
            raise ValueError("invalid memory status")
        if "scope_type" in values:
            scope_type, scope_id = self._normalize_scope(
                str(values["scope_type"]), values.get("scope_id")
            )
            values["scope_type"] = scope_type
            values["scope_id"] = scope_id
        for field in ("confidence", "importance"):
            if field in values:
                values[field] = _clamp(float(values[field]))
        values["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE memories SET {assignments} WHERE id = ?",
                [*values.values(), memory_id],
            )
            if cursor.rowcount == 0:
                raise KeyError(memory_id)
            connection.commit()
            return self.get(memory_id, connection=connection)

    def forget(self, memory_id: int) -> MemoryRecord:
        return self.update(memory_id, status="forgotten")

    def expire_due(self) -> int:
        now = _utc_now()
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE memories SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, now),
            )
            connection.commit()
            return cursor.rowcount

    def sources(self, memory_id: int) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, conversation, excerpt, created_at
                FROM memory_sources WHERE memory_id = ? ORDER BY id DESC
                """,
                (memory_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        self.expire_due()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) count FROM memories GROUP BY status"
            ).fetchall()
            result = {status: 0 for status in MEMORY_STATUSES}
            result.update({str(row["status"]): int(row["count"]) for row in rows})
            result["total"] = sum(result.values())
            return result

    def enqueue_learning_message(
        self,
        conversation: str,
        scope_type: str,
        scope_id: str | None,
        timestamp: int,
        sender: str,
        user_id: int | None,
        content: str,
    ) -> int:
        scope_type, scope_id = self._normalize_scope(scope_type, scope_id)
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_learning_queue (
                    conversation, scope_type, scope_id, timestamp,
                    sender, user_id, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation,
                    scope_type,
                    scope_id,
                    timestamp,
                    sender,
                    user_id,
                    content,
                    _utc_now(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def learning_queue_size(self, conversation: str) -> int:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) count FROM memory_learning_queue WHERE conversation = ?",
                (conversation,),
            ).fetchone()
            return int(row["count"])

    def learning_batch(self, conversation: str, limit: int) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_learning_queue
                WHERE conversation = ? ORDER BY id LIMIT ?
                """,
                (conversation, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def complete_learning_batch(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                f"DELETE FROM memory_learning_queue WHERE id IN ({placeholders})",
                ids,
            )
            connection.commit()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    subject_user_id INTEGER,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT,
                    source_conversation TEXT NOT NULL,
                    source_excerpt TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    explicit INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    supersedes_id INTEGER REFERENCES memories(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memories_scope
                    ON memories(scope_type, scope_id, status);
                CREATE INDEX IF NOT EXISTS idx_memories_subject
                    ON memories(subject_user_id, status);
                CREATE INDEX IF NOT EXISTS idx_memories_key
                    ON memories(normalized_key, kind, subject_user_id);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    conversation TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_learning_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT,
                    timestamp INTEGER NOT NULL,
                    sender TEXT NOT NULL,
                    user_id INTEGER,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_learning_conversation
                    ON memory_learning_queue(conversation, id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _normalize_scope(scope_type: str, scope_id: Any) -> tuple[str, str | None]:
        if scope_type not in SCOPE_TYPES:
            raise ValueError("invalid memory scope")
        if scope_type == "global":
            return scope_type, None
        normalized_id = str(scope_id or "").strip()
        if not normalized_id:
            raise ValueError("non-global memory requires scope_id")
        return scope_type, normalized_id

    @staticmethod
    def _normalize_candidate(candidate: MemoryCandidate) -> MemoryCandidate:
        kind = candidate.kind if candidate.kind in MEMORY_KINDS else "topic"
        content = " ".join(candidate.content.split()).strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        key = re.sub(r"[^\w:\-\.]+", "_", candidate.normalized_key.strip().casefold())
        if not key:
            key = f"content:{hashlib.sha256(content.encode()).hexdigest()[:20]}"
        expires_days = candidate.expires_days
        if expires_days is not None:
            expires_days = min(3650, max(1, int(expires_days)))
        return MemoryCandidate(
            kind=kind,
            content=content[:2000],
            normalized_key=key[:200],
            subject_user_id=candidate.subject_user_id,
            confidence=_clamp(candidate.confidence),
            importance=_clamp(candidate.importance),
            expires_days=expires_days,
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            normalized_key=str(row["normalized_key"]),
            subject_user_id=row["subject_user_id"],
            scope_type=str(row["scope_type"]),
            scope_id=row["scope_id"],
            source_conversation=str(row["source_conversation"]),
            source_excerpt=str(row["source_excerpt"]),
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            status=str(row["status"]),
            explicit=bool(row["explicit"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_accessed_at=row["last_accessed_at"],
            access_count=int(row["access_count"]),
            expires_at=row["expires_at"],
            supersedes_id=row["supersedes_id"],
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(UTC)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _content_key(content: str) -> str:
    return re.sub(r"\s+", "", content).casefold()


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    cjk = re.findall(r"[\u3400-\u9fff]", lowered)
    words.update(cjk)
    words.update("".join(cjk[index : index + 2]) for index in range(len(cjk) - 1))
    return words


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))
