from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models import utc_now
from .models import (
    AgentSoulPayload,
    DEFAULT_AGENT_SOUL_CORE,
    LEGACY_DEFAULT_AGENT_SOUL_CORE_PREFIXES,
    MemoryAuditPayload,
    MemoryEventPayload,
    MemoryNotePayload,
    MemoryProfilePayload,
    MemoryStatePayload,
)


_DB_PATH: Path | None = None
DEFAULT_ID = "default"


def init_db(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_profile (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                manual_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_soul (
                id TEXT PRIMARY KEY,
                core TEXT NOT NULL DEFAULT '',
                side_notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                side_notes_updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_notes (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'manual',
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_audit (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'system',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memory_notes_status ON memory_notes(status);
            CREATE INDEX IF NOT EXISTS idx_memory_events_created_at ON memory_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_audit_created_at ON memory_audit(created_at);
            """
        )
        now = utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_profile (id, content, updated_at)
            VALUES (?, '', ?)
            """,
            (DEFAULT_ID, now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_soul (id, core, side_notes, updated_at)
            VALUES (?, ?, '', ?)
            """,
            (DEFAULT_ID, DEFAULT_AGENT_SOUL_CORE, now),
        )
        _migrate_default_agent_soul_core(conn, now)
        conn.commit()
    finally:
        conn.close()


def _migrate_default_agent_soul_core(conn: sqlite3.Connection, now: str) -> None:
    row = conn.execute("SELECT core FROM agent_soul WHERE id = ?", (DEFAULT_ID,)).fetchone()
    if row is None:
        return
    core = str(row["core"])
    is_empty = not core.strip()
    is_legacy_default = any(
        core.startswith(prefix) for prefix in LEGACY_DEFAULT_AGENT_SOUL_CORE_PREFIXES
    )
    if core == DEFAULT_AGENT_SOUL_CORE or (not is_empty and not is_legacy_default):
        return
    manual_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM memory_audit
        WHERE target_kind = 'agent_soul'
            AND target_id = ?
            AND source = 'manual'
        """,
        (DEFAULT_ID,),
    ).fetchone()["count"]
    if manual_count:
        return
    conn.execute(
        """
        UPDATE agent_soul
        SET core = ?, updated_at = ?
        WHERE id = ?
        """,
        (DEFAULT_AGENT_SOUL_CORE, now, DEFAULT_ID),
    )


def _conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("memory store not initialized; call init_db first")
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _json_loads(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _row_to_profile(row: sqlite3.Row | None) -> MemoryProfilePayload:
    if row is None:
        return MemoryProfilePayload()
    return MemoryProfilePayload(
        content=row["content"],
        updatedAt=row["updated_at"],
        manualUpdatedAt=row["manual_updated_at"],
    )


def _row_to_agent_soul(row: sqlite3.Row | None) -> AgentSoulPayload:
    if row is None:
        return AgentSoulPayload()
    return AgentSoulPayload(
        core=row["core"],
        sideNotes=row["side_notes"],
        updatedAt=row["updated_at"],
        sideNotesUpdatedAt=row["side_notes_updated_at"],
    )


def _row_to_note(row: sqlite3.Row) -> MemoryNotePayload:
    return MemoryNotePayload(
        id=row["id"],
        text=row["text"],
        tags=_json_loads(row["tags_json"], []),
        source=row["source"],
        confidence=float(row["confidence"]),
        status=row["status"],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    )


def _row_to_audit(row: sqlite3.Row) -> MemoryAuditPayload:
    return MemoryAuditPayload(
        id=row["id"],
        action=row["action"],
        targetKind=row["target_kind"],
        targetId=row["target_id"],
        summary=row["summary"],
        source=row["source"],
        createdAt=row["created_at"],
    )


def _row_to_event(row: sqlite3.Row) -> MemoryEventPayload:
    return MemoryEventPayload(
        id=row["id"],
        source=row["source"],
        conversationId=row["conversation_id"],
        requestId=row["request_id"],
        summary=row["summary"],
        content=row["content"],
        payload=_json_loads(row["payload_json"], {}),
        createdAt=row["created_at"],
    )


def append_audit(
    action: str,
    target_kind: str,
    target_id: str = "",
    summary: str = "",
    source: str = "system",
) -> str:
    audit_id = f"audit-{uuid4().hex}"
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO memory_audit (
                id, action, target_kind, target_id, summary, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (audit_id, action, target_kind, target_id, summary, source, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()
    return audit_id


def get_state(
    *,
    include_archived: bool = True,
    audit_limit: int = 80,
    event_limit: int = 20,
) -> MemoryStatePayload:
    conn = _conn()
    try:
        profile = _row_to_profile(
            conn.execute("SELECT * FROM memory_profile WHERE id = ?", (DEFAULT_ID,)).fetchone()
        )
        agent_soul = _row_to_agent_soul(
            conn.execute("SELECT * FROM agent_soul WHERE id = ?", (DEFAULT_ID,)).fetchone()
        )
        if include_archived:
            note_rows = conn.execute(
                "SELECT * FROM memory_notes ORDER BY status ASC, updated_at DESC"
            ).fetchall()
        else:
            note_rows = conn.execute(
                """
                SELECT * FROM memory_notes
                WHERE status = 'active'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        audit_rows = conn.execute(
            """
            SELECT * FROM memory_audit
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (audit_limit,),
        ).fetchall()
        event_rows = conn.execute(
            """
            SELECT * FROM memory_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (event_limit,),
        ).fetchall()
        return MemoryStatePayload(
            profile=profile,
            notes=[_row_to_note(row) for row in note_rows],
            agentSoul=agent_soul,
            audit=[_row_to_audit(row) for row in audit_rows],
            events=[_row_to_event(row) for row in event_rows],
        )
    finally:
        conn.close()


def get_event(event_id: str) -> MemoryEventPayload | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM memory_events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None
    finally:
        conn.close()


def update_profile(content: str, *, source: str = "system", manual: bool = False) -> None:
    now = utc_now()
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE memory_profile
            SET content = ?, updated_at = ?, manual_updated_at = CASE WHEN ? THEN ? ELSE manual_updated_at END
            WHERE id = ?
            """,
            (content, now, 1 if manual else 0, now, DEFAULT_ID),
        )
        conn.execute(
            """
            INSERT INTO memory_audit (
                id, action, target_kind, target_id, summary, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit-{uuid4().hex}",
                "update",
                "profile",
                DEFAULT_ID,
                "用户画像已更新。",
                source,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_agent_soul(
    *,
    core: str | None = None,
    side_notes: str | None = None,
    source: str = "system",
) -> None:
    now = utc_now()
    conn = _conn()
    try:
        current = conn.execute("SELECT * FROM agent_soul WHERE id = ?", (DEFAULT_ID,)).fetchone()
        next_core = core if core is not None else (current["core"] if current else "")
        next_side_notes = (
            side_notes if side_notes is not None else (current["side_notes"] if current else "")
        )
        conn.execute(
            """
            UPDATE agent_soul
            SET core = ?, side_notes = ?, updated_at = ?,
                side_notes_updated_at = CASE WHEN ? THEN ? ELSE side_notes_updated_at END
            WHERE id = ?
            """,
            (next_core, next_side_notes, now, 1 if side_notes is not None else 0, now, DEFAULT_ID),
        )
        conn.execute(
            """
            INSERT INTO memory_audit (
                id, action, target_kind, target_id, summary, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit-{uuid4().hex}",
                "update",
                "agent_soul",
                DEFAULT_ID,
                "Soul 已更新。",
                source,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_note(note: MemoryNotePayload, *, source: str = "system") -> None:
    now = utc_now()
    conn = _conn()
    try:
        existing = conn.execute("SELECT * FROM memory_notes WHERE id = ?", (note.id,)).fetchone()
        created_at = existing["created_at"] if existing else note.created_at
        conn.execute(
            """
            INSERT INTO memory_notes (
                id, text, tags_json, source, confidence, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                tags_json = excluded.tags_json,
                source = excluded.source,
                confidence = excluded.confidence,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                note.id,
                note.text,
                _json_dumps(note.tags),
                note.source or source,
                float(note.confidence),
                note.status,
                created_at,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO memory_audit (
                id, action, target_kind, target_id, summary, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit-{uuid4().hex}",
                "update" if existing else "create",
                "note",
                note.id,
                note.text[:240],
                source,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def archive_note(note_id: str, *, source: str = "system", reason: str = "") -> bool:
    now = utc_now()
    conn = _conn()
    try:
        cursor = conn.execute(
            """
            UPDATE memory_notes
            SET status = 'archived', updated_at = ?
            WHERE id = ?
            """,
            (now, note_id),
        )
        if cursor.rowcount:
            conn.execute(
                """
                INSERT INTO memory_audit (
                    id, action, target_kind, target_id, summary, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"audit-{uuid4().hex}",
                    "archive",
                    "note",
                    note_id,
                    reason or "用户笔记已归档。",
                    source,
                    now,
                ),
            )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def append_event(
    *,
    source: str,
    conversation_id: str = "",
    request_id: str = "",
    summary: str = "",
    content: str = "",
    payload: dict[str, Any] | None = None,
) -> str:
    event_id = f"event-{uuid4().hex}"
    now = utc_now()
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO memory_events (
                id, source, conversation_id, request_id, summary, content, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                source,
                conversation_id,
                request_id,
                summary,
                content,
                _json_dumps(payload or {}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO memory_audit (
                id, action, target_kind, target_id, summary, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit-{uuid4().hex}",
                "create",
                "event",
                event_id,
                summary or "已保存原始记忆事件。",
                source,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return event_id
