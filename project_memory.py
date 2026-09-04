"""
JARVIS project memory (Section 9).

Distinct from memory.py's conversation history: this stores durable
FACTS about a project — name, stack, structure, important files,
known issues, decisions, TODOs — not a transcript of every message.
Section 9 is explicit that this must not become "store every
conversation" (that's what memory.py's conversations table already
does); this module only ever stores what's explicitly saved as a
project-memory fact via save_fact(), plus what project_detector.py
determines automatically about structure/stack.

Uses the same SQLite file as memory.py (config.DB_PATH) via a
separate table set, rather than a second database — one file to
back up, one connection pattern to reason about.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_project_memory_db():
    """Create project-memory tables if they don't exist. Safe to call every startup."""
    conn = get_connection()
    cursor = conn.cursor()

    # One row per known project. path is the unique key — a project is
    # identified by its root directory.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            path          TEXT    NOT NULL UNIQUE,
            name          TEXT    NOT NULL,
            purpose       TEXT    DEFAULT '',
            technologies  TEXT    DEFAULT '[]',
            structure     TEXT    DEFAULT '{}',
            first_seen    TEXT    NOT NULL,
            last_seen     TEXT    NOT NULL
        )
    """)

    # Freeform facts about a project: decisions, TODOs, known issues,
    # previous fixes, current task, important files — anything Section
    # 9 lists. `kind` lets callers filter (e.g. show only TODOs)
    # without needing a table per fact type.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_facts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL,
            kind        TEXT    NOT NULL CHECK(kind IN (
                            'important_file', 'dependency', 'decision',
                            'todo', 'known_issue', 'previous_fix',
                            'current_task', 'note'
                        )),
            content     TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)

    conn.commit()
    conn.close()
    print("[JARVIS] Project memory database initialised.")


# ── Projects ────────────────────────────────────────────────
def upsert_project(
    path: str,
    name: str,
    purpose: str = "",
    technologies: Optional[list[str]] = None,
    structure: Optional[dict] = None,
) -> dict:
    """
    Create or update the project record for `path`. Called by
    project_detector.py whenever it scans a directory — cheap to call
    repeatedly, since it's an upsert keyed on path.
    """
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    existing = cursor.execute(
        "SELECT id, first_seen FROM projects WHERE path = ?", (path,)
    ).fetchone()

    tech_json = json.dumps(technologies or [])
    struct_json = json.dumps(structure or {})

    if existing:
        cursor.execute("""
            UPDATE projects
            SET name = ?, purpose = ?, technologies = ?, structure = ?, last_seen = ?
            WHERE path = ?
        """, (name, purpose, tech_json, struct_json, now, path))
        project_id = existing["id"]
    else:
        cursor.execute("""
            INSERT INTO projects (path, name, purpose, technologies, structure, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (path, name, purpose, tech_json, struct_json, now, now))
        project_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return get_project(path)


def get_project(path: str) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result["technologies"] = json.loads(result["technologies"])
    result["structure"] = json.loads(result["structure"])
    return result


def list_projects() -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM projects ORDER BY last_seen DESC").fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["technologies"] = json.loads(d["technologies"])
        d["structure"] = json.loads(d["structure"])
        results.append(d)
    return results


# ── Facts ───────────────────────────────────────────────────
_VALID_KINDS = {
    "important_file", "dependency", "decision", "todo",
    "known_issue", "previous_fix", "current_task", "note",
}


def save_fact(project_path: str, kind: str, content: str) -> dict:
    """
    Save one discrete fact about a project. This is the only way facts
    enter project memory — never called automatically per-message; the
    caller (a save_project_memory tool call, or project_detector.py for
    structural facts) always decides explicitly that something is worth
    keeping. See module docstring re: not storing every conversation.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid fact kind '{kind}'. Must be one of: {sorted(_VALID_KINDS)}")

    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    project = get_project(project_path)
    if project is None:
        # A fact about a project we haven't formally registered yet —
        # create a minimal project record rather than rejecting the
        # fact outright, so save_fact() never requires the caller to
        # separately call upsert_project() first.
        conn.close()
        upsert_project(project_path, name=project_path.rstrip("/\\").split("/")[-1].split("\\")[-1])
        conn = get_connection()
        cursor = conn.cursor()
        project = get_project(project_path)

    cursor.execute("""
        INSERT INTO project_facts (project_id, kind, content, created_at)
        VALUES (?, ?, ?, ?)
    """, (project["id"], kind, content, now))

    fact_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": fact_id,
        "project_path": project_path,
        "kind": kind,
        "content": content,
        "created_at": now,
    }


def get_facts(project_path: str, kind: Optional[str] = None, limit: int = 100) -> list[dict]:
    project = get_project(project_path)
    if project is None:
        return []

    conn = get_connection()
    cursor = conn.cursor()

    if kind:
        rows = cursor.execute("""
            SELECT * FROM project_facts
            WHERE project_id = ? AND kind = ?
            ORDER BY created_at DESC LIMIT ?
        """, (project["id"], kind, limit)).fetchall()
    else:
        rows = cursor.execute("""
            SELECT * FROM project_facts
            WHERE project_id = ?
            ORDER BY created_at DESC LIMIT ?
        """, (project["id"], limit)).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def search_facts(project_path: str, query: str, limit: int = 20) -> list[dict]:
    """
    Simple substring search over fact content. Deliberately not a full
    FTS/embedding search for this phase — Section 9 asks for "must be
    searchable", not "must be semantic search"; this is the honest,
    working version rather than an unimplemented promise of something
    fancier.
    """
    project = get_project(project_path)
    if project is None:
        return []

    conn = get_connection()
    cursor = conn.cursor()
    like_query = f"%{query}%"
    rows = cursor.execute("""
        SELECT * FROM project_facts
        WHERE project_id = ? AND content LIKE ?
        ORDER BY created_at DESC LIMIT ?
    """, (project["id"], like_query, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_fact(fact_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM project_facts WHERE id = ?", (fact_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
