"""
JARVIS CSE Student Assistant (Phase 5, Sections 5 & 6).

Two kinds of answer, deliberately kept separate:

1. Static reference content (`STATIC_TOPICS`) for a small set of
   extremely common, well-defined CSE comparisons/definitions
   (BFS vs DFS, time-complexity cheat sheet, OOP pillars). These are
   real, accurate, hand-written content — not LLM output — so they're
   instant, work with Ollama offline, and are exactly reproducible in
   tests. This is intentionally a short, curated list, not an attempt
   to replace the LLM for CSE topics generally.

2. Everything else goes through core/llm_orchestrator.py with a
   CSE-specific system prompt (structured, example-driven, matching
   Section 5's command list) plus whatever code-aware context
   (Section 6) core/session_context.py can supply for the current
   session — active project, active file if mentioned, recent
   conversation.

This module never reads file contents itself for the "code-aware"
part — code_analysis.py / debug_mode.py already own that (Section 6:
"Prefer local processing... never silently transmit sensitive source
code externally"); this module only asks context_manager-derived
helpers for *metadata* (which file, which project) and leaves actual
file reads to the existing, deliberately narrow tools.
"""

import re
from typing import Optional

CSE_SYSTEM_PROMPT = """You are JARVIS acting as a CSE (Computer Science & Engineering) student assistant.
The student may ask about: Python, Java, C, C++, JavaScript, TypeScript, SQL, HTML/CSS, Git, Linux/PowerShell,
APIs, data structures, algorithms, OOP, DBMS, operating systems, computer networks, or AI/ML concepts.

Style:
- Be accurate and concrete. Use short code examples where they help.
- For "explain this code" / "find the bug" / "why is this throwing an error" style questions, structure your
  answer as: WHAT IT DOES, ISSUE (if any), FIX (if any).
- For "what's the time complexity" questions, give Big-O for time AND space, and briefly justify it.
- For "explain X like I'm a beginner", avoid jargon and use a small concrete example.
- For "generate test cases", cover: a normal case, an edge case, and an invalid-input case at minimum.
- Keep answers focused — this is a study aid, not an essay.
"""

# Curated, static reference answers for extremely common comparison/
# definition questions. Keys are regex patterns matched against the
# lowercased message; value is the ready-to-return answer text.
STATIC_TOPICS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\bbfs\b.*\bdfs\b|\bdfs\b.*\bbfs\b|compare bfs and dfs", re.IGNORECASE),
        (
            "BFS vs DFS:\n"
            "- BFS (Breadth-First Search): explores level by level using a queue (FIFO). "
            "Finds the shortest path in an unweighted graph. Space: O(V) for the queue in the worst case "
            "(a wide graph). Time: O(V + E).\n"
            "- DFS (Depth-First Search): explores as deep as possible before backtracking, using a stack "
            "(explicit or via recursion). Uses less memory on deep-but-narrow graphs: O(h) where h is the "
            "max depth. Time: O(V + E).\n"
            "- Use BFS for shortest path / level-order problems. Use DFS for exhaustive search, "
            "topological sort, cycle detection, or when memory for a wide graph is a concern."
        ),
    ),
    (
        re.compile(r"time complexity.*(cheat ?sheet|of common|reference)|complexity of common", re.IGNORECASE),
        (
            "Common time complexities, best to worst:\n"
            "O(1) constant, O(log n) logarithmic (binary search), O(n) linear, O(n log n) "
            "(merge sort, quicksort average case), O(n^2) quadratic (bubble/insertion/selection sort, "
            "naive nested loops), O(2^n) exponential (naive recursive Fibonacci), O(n!) factorial "
            "(brute-force permutations)."
        ),
    ),
    (
        re.compile(r"\bpillars? of oop\b|four pillars.*oop|oop pillars", re.IGNORECASE),
        (
            "The four pillars of OOP:\n"
            "1. Encapsulation — bundling data and the methods that operate on it, hiding internal state.\n"
            "2. Abstraction — exposing only what's necessary, hiding implementation detail.\n"
            "3. Inheritance — a class reusing/extending behavior from a parent class.\n"
            "4. Polymorphism — the same interface (method name) behaving differently depending on the "
            "actual object type (overriding/overloading)."
        ),
    ),
    (
        re.compile(r"\bstack\b.*\bqueue\b|\bqueue\b.*\bstack\b.*differ", re.IGNORECASE),
        (
            "Stack vs Queue:\n"
            "- Stack: LIFO (Last-In, First-Out). push/pop from the same end. Used for: function call stacks, "
            "undo history, DFS, expression evaluation.\n"
            "- Queue: FIFO (First-In, First-Out). Enqueue at the back, dequeue from the front. Used for: "
            "task scheduling, BFS, buffering."
        ),
    ),
]


def try_static_answer(message: str) -> Optional[str]:
    for pattern, answer in STATIC_TOPICS:
        if pattern.search(message):
            return answer
    return None


_FILENAME_RE = re.compile(r"\b[\w\-]+\.(py|js|ts|tsx|jsx|java|c|cpp|h|hpp|cs|go|rs|sql|html|css)\b")


def guess_referenced_file(message: str, gathered_context) -> Optional[str]:
    """
    Code-aware lookup (Section 6): prefer an explicit filename in the
    message; otherwise fall back to the most recently mentioned file
    in conversation history, if any. Never invents a file — returns
    None (and callers should ask, not guess) if nothing is findable.
    """
    match = _FILENAME_RE.search(message)
    if match:
        return match.group(0)
    for f in getattr(gathered_context, "relevant_file_paths", []) or []:
        return f
    for msg in reversed(getattr(gathered_context, "recent_messages", []) or []):
        m = _FILENAME_RE.search(msg.get("content") or "")
        if m:
            return m.group(0)
    return None


def build_system_prompt() -> str:
    return CSE_SYSTEM_PROMPT
