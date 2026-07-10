from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models import utc_now
from .models import (
    AgentSoulPayload,
    ConversationContextStatePayload,
    ConversationTurnPayload,
    DEFAULT_AGENT_SOUL_CORE,
    LEGACY_DEFAULT_AGENT_SOUL_CORE_PREFIXES,
    MemoryAuditPayload,
    MemoryEventPayload,
    MemoryNotePayload,
    MemoryProfilePayload,
    MemoryStatePayload,
    MemoryRecallResult,
    LearningCandidate,
    ValidationEvidence,
)


_DB_PATH: Path | None = None
DEFAULT_ID = "default"
DEFAULT_USER_SCOPE = "desktop:default"
WORKSPACE_SCOPE = "workspace"


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
                updated_at TEXT NOT NULL,
                scope_kind TEXT NOT NULL DEFAULT 'user',
                scope_id TEXT NOT NULL DEFAULT 'desktop:default'
            );

            CREATE TABLE IF NOT EXISTS memory_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                scope_kind TEXT NOT NULL DEFAULT 'user',
                scope_id TEXT NOT NULL DEFAULT 'desktop:default'
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

            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                user_content TEXT NOT NULL DEFAULT '',
                assistant_content TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                scope_kind TEXT NOT NULL DEFAULT 'user',
                scope_id TEXT NOT NULL DEFAULT 'desktop:default',
                UNIQUE(conversation_id, request_id)
            );

            CREATE TABLE IF NOT EXISTS conversation_context_state (
                conversation_id TEXT PRIMARY KEY,
                archive_summary TEXT NOT NULL DEFAULT '',
                idle_summary TEXT NOT NULL DEFAULT '',
                idle_through_turn_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_profiles_scoped (
                scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                manual_updated_at TEXT,
                PRIMARY KEY (scope_kind, scope_id)
            );

            CREATE TABLE IF NOT EXISTS learning_candidates (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                title TEXT NOT NULL,
                reason TEXT NOT NULL,
                proposal_json TEXT NOT NULL DEFAULT '{}',
                source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                risk_flags_json TEXT NOT NULL DEFAULT '[]',
                validation_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_scopes (
                conversation_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(
                item_id UNINDEXED,
                item_kind UNINDEXED,
                scope_kind UNINDEXED,
                scope_id UNINDEXED,
                content
            );

            CREATE INDEX IF NOT EXISTS idx_memory_notes_status ON memory_notes(status);
            CREATE INDEX IF NOT EXISTS idx_memory_events_created_at ON memory_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_audit_created_at ON memory_audit(created_at);
            CREATE INDEX IF NOT EXISTS idx_conversation_turns_lookup
                ON conversation_turns(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_memory_notes_scope
                ON memory_notes(scope_kind, scope_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_learning_candidates_status
                ON learning_candidates(status, updated_at);
            """
        )
        _ensure_column(conn, "memory_notes", "scope_kind", "TEXT NOT NULL DEFAULT 'user'")
        _ensure_column(conn, "memory_notes", "scope_id", "TEXT NOT NULL DEFAULT 'desktop:default'")
        _ensure_column(conn, "memory_events", "scope_kind", "TEXT NOT NULL DEFAULT 'user'")
        _ensure_column(conn, "memory_events", "scope_id", "TEXT NOT NULL DEFAULT 'desktop:default'")
        _ensure_column(conn, "conversation_turns", "scope_kind", "TEXT NOT NULL DEFAULT 'user'")
        _ensure_column(conn, "conversation_turns", "scope_id", "TEXT NOT NULL DEFAULT 'desktop:default'")
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
        _migrate_legacy_memory(conn, now)
        _rebuild_search_index(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_legacy_memory(conn: sqlite3.Connection, now: str) -> None:
    row = conn.execute("SELECT content, updated_at, manual_updated_at FROM memory_profile WHERE id = ?", (DEFAULT_ID,)).fetchone()
    if row is not None:
        conn.execute(
            """INSERT OR IGNORE INTO memory_profiles_scoped
               (scope_kind, scope_id, content, updated_at, manual_updated_at)
               VALUES ('user', ?, ?, ?, ?)""",
            (DEFAULT_USER_SCOPE, row["content"], row["updated_at"], row["manual_updated_at"]),
        )
    conn.execute("UPDATE memory_notes SET scope_kind = COALESCE(NULLIF(scope_kind, ''), 'user'), scope_id = COALESCE(NULLIF(scope_id, ''), ?)", (DEFAULT_USER_SCOPE,))
    conn.execute("UPDATE memory_events SET scope_kind = COALESCE(NULLIF(scope_kind, ''), 'user'), scope_id = COALESCE(NULLIF(scope_id, ''), ?)", (DEFAULT_USER_SCOPE,))
    conn.execute("UPDATE conversation_turns SET scope_kind = COALESCE(NULLIF(scope_kind, ''), 'user'), scope_id = COALESCE(NULLIF(scope_id, ''), ?)", (DEFAULT_USER_SCOPE,))


def _rebuild_search_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM memory_search")
    conn.execute("""INSERT INTO memory_search(item_id, item_kind, scope_kind, scope_id, content)
                    SELECT id, 'note', scope_kind, scope_id, text FROM memory_notes WHERE status = 'active'""")
    conn.execute("""INSERT INTO memory_search(item_id, item_kind, scope_kind, scope_id, content)
                    SELECT id, 'event', scope_kind, scope_id, summary || '\n' || content FROM memory_events""")
    conn.execute("""INSERT INTO memory_search(item_id, item_kind, scope_kind, scope_id, content)
                    SELECT CAST(id AS TEXT), 'turn', scope_kind, scope_id, user_content || '\n' || assistant_content FROM conversation_turns""")


def _reindex_item(
    conn: sqlite3.Connection,
    item_id: str,
    item_kind: str,
    scope_kind: str,
    scope_id: str,
    content: str,
) -> None:
    conn.execute("DELETE FROM memory_search WHERE item_id = ? AND item_kind = ?", (item_id, item_kind))
    conn.execute(
        "INSERT INTO memory_search(item_id, item_kind, scope_kind, scope_id, content) VALUES (?, ?, ?, ?, ?)",
        (item_id, item_kind, scope_kind, scope_id, content),
    )


def register_conversation_scope(conversation_id: str, scope_id: str) -> None:
    if not conversation_id or not scope_id:
        return
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO conversation_scopes(conversation_id, scope_id, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET scope_id = excluded.scope_id, updated_at = excluded.updated_at""",
            (conversation_id, scope_id, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def scope_id_for_conversation(conversation_id: str) -> str:
    if not conversation_id:
        return DEFAULT_USER_SCOPE
    conn = _conn()
    try:
        row = conn.execute("SELECT scope_id FROM conversation_scopes WHERE conversation_id = ?", (conversation_id,)).fetchone()
        return str(row["scope_id"]) if row else DEFAULT_USER_SCOPE
    finally:
        conn.close()


def search_history(query: str, *, scope_id: str = DEFAULT_USER_SCOPE, limit: int = 8) -> list[MemoryRecallResult]:
    terms = [term.replace('"', "") for term in query.split() if term.strip()]
    if not terms:
        return []
    match = " OR ".join(f'"{term}"' for term in terms[:8])
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT item_id, item_kind, scope_kind, scope_id, content
               FROM memory_search
               WHERE memory_search MATCH ? AND ((scope_kind = 'user' AND scope_id = ?)
                    OR (scope_kind = 'workspace' AND scope_id = ?))
               ORDER BY rank LIMIT ?""",
            (match, scope_id, WORKSPACE_SCOPE, max(1, min(limit, 20))),
        ).fetchall()
        return [
            MemoryRecallResult(
                id=row["item_id"], kind=row["item_kind"], text=row["content"][:1600],
                source="history", scopeKind=row["scope_kind"], scopeId=row["scope_id"],
                createdAt="", confidence=1.0,
            )
            for row in rows
        ]
    finally:
        conn.close()


def create_learning_candidate(candidate: LearningCandidate) -> LearningCandidate:
    conn = _conn()
    try:
        existing = conn.execute(
            """SELECT id FROM learning_candidates WHERE status = 'pending' AND kind = ? AND scope_kind = ?
               AND scope_id = ? AND title = ?""",
            (candidate.kind, candidate.scope_kind, candidate.scope_id, candidate.title),
        ).fetchone()
        if existing:
            row = conn.execute("SELECT * FROM learning_candidates WHERE id = ?", (existing["id"],)).fetchone()
            assert row is not None
            return _row_to_learning_candidate(row)
        conn.execute(
            """INSERT INTO learning_candidates (
                id, kind, status, scope_kind, scope_id, title, reason, proposal_json,
                source_event_ids_json, risk_flags_json, validation_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            _learning_candidate_values(candidate),
        )
        conn.execute(
            """INSERT INTO memory_audit(id, action, target_kind, target_id, summary, source, created_at)
               VALUES (?, 'create', 'learning_candidate', ?, ?, 'auto:learning', ?)""",
            (f"audit-{uuid4().hex}", candidate.id, candidate.title, utc_now()),
        )
        conn.commit()
        return candidate
    finally:
        conn.close()


def list_learning_candidates(*, status: str | None = None, limit: int = 80) -> list[LearningCandidate]:
    conn = _conn()
    try:
        if status:
            rows = conn.execute("SELECT * FROM learning_candidates WHERE status = ? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM learning_candidates ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_learning_candidate(row) for row in rows]
    finally:
        conn.close()


def set_learning_candidate_status(candidate_id: str, status: str) -> LearningCandidate | None:
    conn = _conn()
    try:
        conn.execute("UPDATE learning_candidates SET status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), candidate_id))
        row = conn.execute("SELECT * FROM learning_candidates WHERE id = ?", (candidate_id,)).fetchone()
        conn.commit()
        return _row_to_learning_candidate(row) if row else None
    finally:
        conn.close()


def update_learning_candidate_validation(
    candidate_id: str,
    validation: ValidationEvidence,
) -> LearningCandidate | None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE learning_candidates SET validation_json = ?, updated_at = ? WHERE id = ?",
            (_json_dumps(validation.model_dump(by_alias=True)), utc_now(), candidate_id),
        )
        row = conn.execute("SELECT * FROM learning_candidates WHERE id = ?", (candidate_id,)).fetchone()
        conn.commit()
        return _row_to_learning_candidate(row) if row else None
    finally:
        conn.close()


def _learning_candidate_values(candidate: LearningCandidate) -> tuple[Any, ...]:
    return (
        candidate.id, candidate.kind, candidate.status, candidate.scope_kind, candidate.scope_id,
        candidate.title, candidate.reason, _json_dumps(candidate.proposal),
        _json_dumps(candidate.source_event_ids), _json_dumps(candidate.risk_flags),
        _json_dumps(candidate.validation.model_dump(by_alias=True)), candidate.created_at, candidate.updated_at,
    )


def _row_to_learning_candidate(row: sqlite3.Row) -> LearningCandidate:
    validation = _json_loads(row["validation_json"], {})
    return LearningCandidate(
        id=row["id"], kind=row["kind"], status=row["status"], scopeKind=row["scope_kind"],
        scopeId=row["scope_id"], title=row["title"], reason=row["reason"],
        proposal=_json_loads(row["proposal_json"], {}),
        sourceEventIds=_json_loads(row["source_event_ids_json"], []),
        riskFlags=_json_loads(row["risk_flags_json"], []),
        validation=ValidationEvidence.model_validate(validation), createdAt=row["created_at"], updatedAt=row["updated_at"],
    )


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
        scopeKind=row["scope_kind"],
        scopeId=row["scope_id"],
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
        scopeKind=row["scope_kind"],
        scopeId=row["scope_id"],
    )


def _row_to_conversation_turn(row: sqlite3.Row) -> ConversationTurnPayload:
    return ConversationTurnPayload(
        id=int(row["id"]),
        conversationId=row["conversation_id"],
        requestId=row["request_id"],
        userContent=row["user_content"],
        assistantContent=row["assistant_content"],
        route=row["route"],
        metadata=_json_loads(row["metadata_json"], {}),
        createdAt=row["created_at"],
        scopeKind=row["scope_kind"],
        scopeId=row["scope_id"],
    )


def _row_to_conversation_context(
    row: sqlite3.Row | None,
    conversation_id: str,
) -> ConversationContextStatePayload:
    if row is None:
        return ConversationContextStatePayload(conversationId=conversation_id)
    return ConversationContextStatePayload(
        conversationId=row["conversation_id"],
        archiveSummary=row["archive_summary"],
        idleSummary=row["idle_summary"],
        idleThroughTurnId=int(row["idle_through_turn_id"]),
        updatedAt=row["updated_at"],
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
    scope_id: str = DEFAULT_USER_SCOPE,
) -> MemoryStatePayload:
    conn = _conn()
    try:
        profile = _row_to_profile(
            conn.execute(
                "SELECT * FROM memory_profiles_scoped WHERE scope_kind = 'user' AND scope_id = ?",
                (scope_id,),
            ).fetchone()
        )
        agent_soul = _row_to_agent_soul(
            conn.execute("SELECT * FROM agent_soul WHERE id = ?", (DEFAULT_ID,)).fetchone()
        )
        if include_archived:
            note_rows = conn.execute(
                """SELECT * FROM memory_notes
                   WHERE (scope_kind = 'user' AND scope_id = ?) OR (scope_kind = 'workspace' AND scope_id = ?)
                   ORDER BY status ASC, updated_at DESC""",
                (scope_id, WORKSPACE_SCOPE),
            ).fetchall()
        else:
            note_rows = conn.execute(
                """
                SELECT * FROM memory_notes
                WHERE status = 'active'
                    AND ((scope_kind = 'user' AND scope_id = ?) OR (scope_kind = 'workspace' AND scope_id = ?))
                ORDER BY updated_at DESC
                """,
                (scope_id, WORKSPACE_SCOPE),
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


def list_conversation_events(
    conversation_id: str,
    *,
    source: str | None = None,
    limit: int = 100,
) -> list[MemoryEventPayload]:
    conn = _conn()
    try:
        if source:
            rows = conn.execute(
                """
                SELECT * FROM memory_events
                WHERE conversation_id = ? AND source = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, source, max(1, int(limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM memory_events
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, max(1, int(limit))),
            ).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]
    finally:
        conn.close()


def upsert_conversation_turn(
    *,
    conversation_id: str,
    request_id: str,
    user_content: str,
    assistant_content: str,
    route: str = "",
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> ConversationTurnPayload:
    now = created_at or utc_now()
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO conversation_turns (
                conversation_id, request_id, user_content, assistant_content,
                route, metadata_json, created_at, scope_kind, scope_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, request_id) DO UPDATE SET
                user_content = excluded.user_content,
                assistant_content = excluded.assistant_content,
                route = excluded.route,
                metadata_json = excluded.metadata_json,
                scope_kind = excluded.scope_kind,
                scope_id = excluded.scope_id
            """,
            (
                conversation_id,
                request_id,
                user_content,
                assistant_content,
                route,
                _json_dumps(metadata or {}),
                now,
                "user",
                scope_id_for_conversation(conversation_id),
            ),
        )
        _reindex_item(
            conn,
            f"{conversation_id}:{request_id}",
            "turn",
            "user",
            scope_id_for_conversation(conversation_id),
            f"{user_content}\n{assistant_content}",
        )
        row = conn.execute(
            """
            SELECT * FROM conversation_turns
            WHERE conversation_id = ? AND request_id = ?
            """,
            (conversation_id, request_id),
        ).fetchone()
        conn.commit()
        assert row is not None
        return _row_to_conversation_turn(row)
    finally:
        conn.close()


def list_conversation_turns(conversation_id: str) -> list[ConversationTurnPayload]:
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM conversation_turns
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()
        return [_row_to_conversation_turn(row) for row in rows]
    finally:
        conn.close()


def delete_conversation_turns(conversation_id: str, turn_ids: list[int]) -> int:
    ids = [int(item) for item in turn_ids if int(item) > 0]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn = _conn()
    try:
        cursor = conn.execute(
            f"""
            DELETE FROM conversation_turns
            WHERE conversation_id = ? AND id IN ({placeholders})
            """,
            (conversation_id, *ids),
        )
        conn.commit()
        return int(cursor.rowcount)
    finally:
        conn.close()


def get_conversation_context_state(
    conversation_id: str,
) -> ConversationContextStatePayload:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM conversation_context_state
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        return _row_to_conversation_context(row, conversation_id)
    finally:
        conn.close()


def update_conversation_context_state(
    conversation_id: str,
    *,
    archive_summary: str | None = None,
    idle_summary: str | None = None,
    idle_through_turn_id: int | None = None,
) -> ConversationContextStatePayload:
    current = get_conversation_context_state(conversation_id)
    next_archive = current.archive_summary if archive_summary is None else archive_summary
    next_idle = current.idle_summary if idle_summary is None else idle_summary
    next_idle_through = (
        current.idle_through_turn_id
        if idle_through_turn_id is None
        else max(0, int(idle_through_turn_id))
    )
    now = utc_now()
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO conversation_context_state (
                conversation_id, archive_summary, idle_summary,
                idle_through_turn_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                archive_summary = excluded.archive_summary,
                idle_summary = excluded.idle_summary,
                idle_through_turn_id = excluded.idle_through_turn_id,
                updated_at = excluded.updated_at
            """,
            (
                conversation_id,
                next_archive,
                next_idle,
                next_idle_through,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return ConversationContextStatePayload(
        conversationId=conversation_id,
        archiveSummary=next_archive,
        idleSummary=next_idle,
        idleThroughTurnId=next_idle_through,
        updatedAt=now,
    )


def update_profile(
    content: str,
    *,
    source: str = "system",
    manual: bool = False,
    scope_id: str = DEFAULT_USER_SCOPE,
) -> None:
    now = utc_now()
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO memory_profiles_scoped(scope_kind, scope_id, content, updated_at, manual_updated_at)
               VALUES ('user', ?, ?, ?, ?)
               ON CONFLICT(scope_kind, scope_id) DO UPDATE SET content = excluded.content,
                   updated_at = excluded.updated_at,
                   manual_updated_at = CASE WHEN ? THEN excluded.manual_updated_at ELSE memory_profiles_scoped.manual_updated_at END""",
            (scope_id, content, now, now if manual else None, 1 if manual else 0),
        )
        conn.execute(
            """
            UPDATE memory_profile
            SET content = ?, updated_at = ?, manual_updated_at = CASE WHEN ? THEN ? ELSE manual_updated_at END
            WHERE id = ? AND ? = ?
            """,
            (content, now, 1 if manual else 0, now, DEFAULT_ID, scope_id, DEFAULT_USER_SCOPE),
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
                id, text, tags_json, source, confidence, status, created_at, updated_at, scope_kind, scope_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                tags_json = excluded.tags_json,
                source = excluded.source,
                confidence = excluded.confidence,
                status = excluded.status,
                updated_at = excluded.updated_at,
                scope_kind = excluded.scope_kind,
                scope_id = excluded.scope_id
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
                note.scope_kind,
                note.scope_id,
            ),
        )
        _reindex_item(conn, note.id, "note", note.scope_kind, note.scope_id, note.text)
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
            conn.execute("DELETE FROM memory_search WHERE item_id = ? AND item_kind = 'note'", (note_id,))
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
    scope_id: str | None = None,
) -> str:
    event_id = f"event-{uuid4().hex}"
    now = utc_now()
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO memory_events (
                id, source, conversation_id, request_id, summary, content, payload_json, created_at, scope_kind, scope_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "user",
                scope_id or scope_id_for_conversation(conversation_id),
            ),
        )
        _reindex_item(conn, event_id, "event", "user", scope_id or scope_id_for_conversation(conversation_id), f"{summary}\n{content}")
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
