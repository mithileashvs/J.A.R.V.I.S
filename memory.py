import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional
from config import DB_PATH


# ── Database Setup ─────────────────────────────────────────
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    cursor = conn.cursor()

    # Conversations table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL,
            role        TEXT    NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
            content     TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL,
            metadata    TEXT    DEFAULT '{}'
        )
    """)

    # Sessions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT    PRIMARY KEY,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            title       TEXT    DEFAULT 'JARVIS Session',
            message_count INTEGER DEFAULT 0
        )
    """)

    # System events table (tracks agent starts, errors, status changes)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    print("[JARVIS] Database initialised.")


# ── Session Management ─────────────────────────────────────
def create_session(session_id: str) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    cursor.execute("""
        INSERT OR IGNORE INTO sessions (id, created_at, updated_at, title)
        VALUES (?, ?, ?, ?)
    """, (session_id, now, now, "JARVIS Session"))

    conn.commit()
    conn.close()
    return {"session_id": session_id, "created_at": now}


def get_session(session_id: str) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)
    return None


def list_sessions() -> list:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM sessions
        ORDER BY updated_at DESC
        LIMIT 50
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Message Management ─────────────────────────────────────
def save_message(
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[dict] = None
) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    meta = json.dumps(metadata or {})

    # Ensure session exists
    create_session(session_id)

    cursor.execute("""
        INSERT INTO conversations (session_id, role, content, timestamp, metadata)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, role, content, now, meta))

    # Update session message count and timestamp
    cursor.execute("""
        UPDATE sessions
        SET updated_at = ?, message_count = message_count + 1
        WHERE id = ?
    """, (now, session_id))

    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "id":         msg_id,
        "session_id": session_id,
        "role":       role,
        "content":    content,
        "timestamp":  now,
        "metadata":   metadata or {}
    }


def get_history(session_id: str, limit: int = 50) -> list:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM conversations
        WHERE session_id = ?
        ORDER BY timestamp ASC
        LIMIT ?
    """, (session_id, limit))

    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        row_dict = dict(r)
        row_dict["metadata"] = json.loads(row_dict.get("metadata", "{}"))
        result.append(row_dict)
    return result


def get_all_history(limit: int = 100) -> list:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.*, s.title as session_title
        FROM conversations c
        JOIN sessions s ON c.session_id = s.id
        ORDER BY c.timestamp DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        row_dict = dict(r)
        row_dict["metadata"] = json.loads(row_dict.get("metadata", "{}"))
        result.append(row_dict)
    return result


def clear_session_history(session_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM conversations WHERE session_id = ?",
        (session_id,)
    )
    cursor.execute(
        "UPDATE sessions SET message_count = 0 WHERE id = ?",
        (session_id,)
    )

    conn.commit()
    conn.close()
    return True


# ── System Events ──────────────────────────────────────────
def log_event(event_type: str, message: str):
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    cursor.execute("""
        INSERT INTO system_events (event_type, message, timestamp)
        VALUES (?, ?, ?)
    """, (event_type, message, now))

    conn.commit()
    conn.close()


def get_recent_events(limit: int = 20, event_type_prefix: Optional[str] = None) -> list:
    """
    Feature 16 — the read side of the audit log. `event_type_prefix` is
    optional and backward compatible (existing callers passing just
    `limit` are unaffected); it lets a caller like main.py's "recent
    activity" command ask specifically for `workflow:`-prefixed events
    without introducing a second logging system or table.
    """
    conn = get_connection()
    cursor = conn.cursor()

    if event_type_prefix:
        cursor.execute("""
            SELECT * FROM system_events
            WHERE event_type LIKE ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (f"{event_type_prefix}%", limit))
    else:
        cursor.execute("""
            SELECT * FROM system_events
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))

    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


class AuditLogError(Exception):
    """Feature 16 — raised when the audit log can't be read/cleared due
    to a genuine storage problem, so callers (main.py) can tell that
    apart from "the log is just empty" and report it honestly instead
    of pretending the clear succeeded."""


def clear_events() -> int:
    """
    Feature 16 — explicitly, safely clear the workflow audit log.
    Never called implicitly: main.py only reaches this after the user
    has explicitly confirmed the action in a separate turn. Returns the
    number of rows removed (0 for an already-empty log — that's a
    normal, non-error outcome, not treated as a failure).
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM system_events")
        count = cursor.fetchone()["c"]
        if count:
            cursor.execute("DELETE FROM system_events")
            conn.commit()
        conn.close()
        return count
    except sqlite3.Error as e:
        raise AuditLogError(f"Could not clear the audit log: {e}") from e


# ── Stats ──────────────────────────────────────────────────
def get_stats() -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM conversations")
    total_messages = cursor.fetchone()["total"]

    cursor.execute("SELECT COUNT(*) as total FROM sessions")
    total_sessions = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT COUNT(*) as total FROM conversations
        WHERE timestamp >= datetime('now', '-1 day')
    """)
    messages_today = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT content, timestamp FROM conversations
        WHERE role = 'user'
        ORDER BY timestamp DESC
        LIMIT 1
    """)
    last_row = cursor.fetchone()
    last_message = dict(last_row) if last_row else None

    conn.close()

    return {
        "total_messages":  total_messages,
        "total_sessions":  total_sessions,
        "messages_today":  messages_today,
        "last_message":    last_message,
    }