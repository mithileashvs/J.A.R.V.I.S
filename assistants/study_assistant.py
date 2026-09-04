"""
JARVIS Study Assistant (Phase 5, Section 9).

Study-topic tracking is real, persisted local state (SQLite, via
memory.py's existing get_connection() — a new table, not a new
database/connection mechanism, so this doesn't duplicate Phase 1/2's
storage system). Everything else (explanations, quizzes, flashcards,
revision plans) is prompt construction for core/llm_orchestrator.py —
this module does not fabricate quiz answers or flashcard content
itself, since doing so without an LLM would mean either hard-coding a
tiny fixed quiz bank (misleading — "generate a quiz" implies real
generation) or guessing, neither of which Section 23 allows
("do not create fake implementations").
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from memory import get_connection

logger = logging.getLogger("jarvis-study")

LEVELS = ["BEGINNER", "INTERMEDIATE", "ADVANCED"]


def init_study_db() -> None:
    """
    Idempotent, matches memory.init_db()'s / project_memory's
    CREATE TABLE IF NOT EXISTS pattern. Must be called once at startup
    the same way those are — see main.py's lifespan() wiring.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS study_topics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT    NOT NULL,
            topic       TEXT    NOT NULL,
            level       TEXT    NOT NULL DEFAULT 'BEGINNER',
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()


@dataclass
class StudyTopic:
    id: int
    session_id: str
    topic: str
    level: str
    created_at: str
    updated_at: str


def start_topic(session_id: str, topic: str, level: str = "BEGINNER") -> StudyTopic:
    level = level.upper() if level.upper() in LEVELS else "BEGINNER"
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO study_topics (session_id, topic, level, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, topic, level, now, now),
    )
    topic_id = cur.lastrowid
    conn.commit()
    conn.close()
    return StudyTopic(id=topic_id, session_id=session_id, topic=topic, level=level, created_at=now, updated_at=now)


def advance_level(session_id: str, topic: str) -> Optional[StudyTopic]:
    """Move a tracked topic up one level (BEGINNER -> INTERMEDIATE -> ADVANCED), capped at ADVANCED."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM study_topics WHERE session_id = ? AND topic = ? ORDER BY updated_at DESC LIMIT 1",
        (session_id, topic),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        return None
    current_idx = LEVELS.index(row["level"]) if row["level"] in LEVELS else 0
    new_level = LEVELS[min(current_idx + 1, len(LEVELS) - 1)]
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "UPDATE study_topics SET level = ?, updated_at = ? WHERE id = ?",
        (new_level, now, row["id"]),
    )
    conn.commit()
    conn.close()
    return StudyTopic(id=row["id"], session_id=session_id, topic=topic, level=new_level,
                       created_at=row["created_at"], updated_at=now)


def get_topics(session_id: str, limit: int = 20) -> list[StudyTopic]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM study_topics WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?",
        (session_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [StudyTopic(**dict(r)) for r in rows]


def get_current_level(session_id: str, topic: str) -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT level FROM study_topics WHERE session_id = ? AND topic = ? ORDER BY updated_at DESC LIMIT 1",
        (session_id, topic),
    )
    row = cur.fetchone()
    conn.close()
    return row["level"] if row else "BEGINNER"


# ── Prompt builders (system prompts for core/llm_orchestrator.run) ──

def teach_prompt(topic: str, level: str = "BEGINNER") -> str:
    level = level.upper() if level.upper() in LEVELS else "BEGINNER"
    return (
        f"You are JARVIS teaching Computer Science topic '{topic}' at {level} level.\n"
        f"- BEGINNER: plain language, one small concrete example, no jargon without defining it.\n"
        f"- INTERMEDIATE: assume the basics are known, introduce terminology and trade-offs.\n"
        f"- ADVANCED: assume fluency, discuss edge cases, performance, and real-world nuance.\n"
        f"End with one short question checking understanding, so the student can say 'quiz me' next."
    )


def quiz_prompt(topic: str, level: str = "BEGINNER", harder: bool = False) -> str:
    level = level.upper() if level.upper() in LEVELS else "BEGINNER"
    difficulty_note = "Make this noticeably harder than a typical question at this level." if harder else ""
    return (
        f"You are JARVIS quizzing a student on '{topic}' at {level} level. {difficulty_note}\n"
        "Ask exactly ONE question. Do not reveal the answer yet. Wait for the student's answer before "
        "explaining whether it was right or wrong and why."
    )


def explain_wrong_answer_prompt(topic: str, question: str, student_answer: str) -> str:
    return (
        f"You are JARVIS. The student was asked about '{topic}':\n"
        f"Question: {question}\n"
        f"Student's answer: {student_answer}\n"
        "Explain clearly why this answer is wrong (or right, if it actually is — check carefully first), "
        "then give the correct reasoning."
    )


def grade_and_explain_prompt(topic: str, question: str, student_answer: str) -> str:
    """
    Phase 6 Feature 13 (guided study session workflow) — a stricter
    variant of explain_wrong_answer_prompt() above for programmatic use:
    the workflow needs a machine-checkable correctness signal (to
    decide whether the next round gets harder or easier), so this one
    requires the reply's first line to name the verdict explicitly.
    """
    return (
        f"You are JARVIS grading a student's answer on '{topic}'.\n"
        f"Question: {question}\n"
        f"Student's answer: {student_answer}\n"
        "Check carefully, then reply with the single word CORRECT or INCORRECT on the first line, "
        "followed by a blank line, then a short explanation of why, and the correct reasoning if it "
        "was wrong."
    )


def flashcards_prompt(topic: str, count: int = 8) -> str:
    return (
        f"Generate {count} flashcards for studying '{topic}'. "
        "Format each as 'Q: ...' on one line and 'A: ...' on the next, separated by a blank line. "
        "Keep answers concise (1-2 sentences)."
    )


def summarize_notes_prompt(notes_text: str) -> str:
    return (
        "Summarize the following study notes into concise bullet points, grouped by sub-topic where it makes "
        f"sense:\n\n{notes_text}"
    )


def revision_plan_prompt(subject: str, days_available: Optional[int] = None) -> str:
    horizon = f"over the next {days_available} day(s)" if days_available else "before the exam"
    return (
        f"Create a revision plan for '{subject}' {horizon}. Break it into a day-by-day schedule, "
        "prioritizing topics likely to carry the most exam weight. Keep each day's plan to 3-5 bullet points."
    )


def viva_questions_prompt(topic: str, count: int = 5) -> str:
    return (
        f"Generate {count} viva/oral-exam style questions on '{topic}', ordered from foundational to "
        "probing/follow-up depth, as if an examiner were asking them one after another."
    )


def practice_questions_prompt(subject: str, count: int = 5) -> str:
    """
    Phase 6 Feature 13 (CSE Exam Prep workflow) — distinct from
    viva_questions_prompt() above: these are written practice
    questions with an answer key, for self-checking while revising,
    not oral-exam-style follow-up questions.
    """
    return (
        f"Generate {count} written practice exam questions on '{subject}', covering a mix of "
        "difficulty levels. After the questions, include a separate 'Answer Key' section with a "
        "concise correct answer for each."
    )


def coding_exercise_prompt(topic: str, level: str = "BEGINNER") -> str:
    level = level.upper() if level.upper() in LEVELS else "BEGINNER"
    return (
        f"Generate one coding exercise on '{topic}' at {level} level. Include: a clear problem statement, "
        "example input/output, and constraints. Do NOT include the solution unless asked."
    )
