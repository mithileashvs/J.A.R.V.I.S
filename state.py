"""
JARVIS backend state machine.

Single source of truth for what JARVIS is currently doing. The backend
owns this state; the frontend only ever reflects it, never invents it
locally. This closes the gap where EXECUTING (a tool actually running)
was previously invisible to the UI, and gives ERROR a real place to
live instead of being represented ad-hoc by log lines.

Usage:
    from state import state_manager, JarvisState

    await state_manager.set_state(JarvisState.THINKING, broadcast_fn)
"""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("jarvis-state")

BroadcastFn = Callable[[dict], Awaitable[None]]


class JarvisState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"


# Allowed transitions. Kept explicit (rather than "anything goes") so a
# bug elsewhere can't silently leave the UI in a state that doesn't
# make sense — e.g. jumping straight from LISTENING to SPEAKING with no
# THINKING/EXECUTING step in between would hide real processing time
# from the user in a way that looks like a stall.
#
# Two entry points reach THINKING/EXECUTING from IDLE:
#   - Voice: button press -> LISTENING -> (speech ends) -> THINKING
#   - Text (/chat REST, or a directly-invoked tool run): no mic
#     involved, so THINKING/EXECUTING can start straight from IDLE.
# Both are legitimate "JARVIS starts working" transitions, not a
# relaxation of the state machine — LISTENING remains mandatory
# specifically for anything that actually involves the microphone.
_ALLOWED_TRANSITIONS: dict[JarvisState, set[JarvisState]] = {
    JarvisState.IDLE: {
        JarvisState.LISTENING, JarvisState.THINKING,
        JarvisState.EXECUTING, JarvisState.ERROR,
    },
    JarvisState.LISTENING: {JarvisState.THINKING, JarvisState.IDLE, JarvisState.ERROR},
    JarvisState.THINKING:  {JarvisState.EXECUTING, JarvisState.SPEAKING, JarvisState.IDLE, JarvisState.ERROR},
    JarvisState.EXECUTING: {JarvisState.SPEAKING, JarvisState.THINKING, JarvisState.IDLE, JarvisState.ERROR},
    JarvisState.SPEAKING:  {JarvisState.LISTENING, JarvisState.IDLE, JarvisState.ERROR},
    JarvisState.ERROR:     {JarvisState.IDLE, JarvisState.LISTENING},
}


class StateManager:
    """
    Tracks JARVIS's current backend state and broadcasts every change.

    Not tied to any one session — a single JARVIS instance has one
    "what am I doing right now" state at a time, matching the fact
    there's exactly one voice agent process.
    """

    def __init__(self) -> None:
        self._state: JarvisState = JarvisState.IDLE
        self._changed_at: datetime = datetime.now(timezone.utc).replace(tzinfo=None)
        self._detail: Optional[str] = None

    @property
    def state(self) -> JarvisState:
        return self._state

    def as_dict(self) -> dict:
        return {
            "state": self._state.value,
            "changed_at": self._changed_at.isoformat(),
            "detail": self._detail,
        }

    async def set_state(
        self,
        new_state: JarvisState,
        broadcast: Optional[BroadcastFn] = None,
        detail: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """
        Transition to new_state. Returns True if the transition happened.

        force=True skips the allowed-transition check — reserved for
        recovering into IDLE/ERROR from genuinely unexpected situations
        (e.g. the agent process died mid-turn), not for routine flow.

        A same-state call (new_state == current state) is always
        allowed regardless of the transition table and without needing
        force=True — it isn't a real transition, just a progress/detail
        update within the state JARVIS is already in (e.g. a multi-step
        debug investigation broadcasting "step 3/8: ..." while staying
        in EXECUTING throughout). Gating that behind the transition
        table would mean only the first of several same-state updates
        in a row ever took effect, silently freezing the UI's progress
        display for everything after it.
        """
        is_same_state = new_state == self._state
        if not force and not is_same_state and new_state not in _ALLOWED_TRANSITIONS.get(self._state, set()):
            logger.warning(
                f"[state] Rejected transition {self._state.value} -> {new_state.value} "
                f"(not in allowed set; use force=True if this is a genuine recovery path)"
            )
            return False

        if not is_same_state:
            logger.info(f"[state] {self._state.value} -> {new_state.value}")
        self._state = new_state
        self._changed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self._detail = detail

        if broadcast is not None:
            try:
                await broadcast({
                    "type": "backend_state",
                    **self.as_dict(),
                })
            except Exception as e:
                logger.warning(f"[state] Broadcast failed: {e}")

        return True

    async def set_error(
        self,
        broadcast: Optional[BroadcastFn] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Force into ERROR from any state — errors can happen anywhere."""
        await self.set_state(JarvisState.ERROR, broadcast, detail=detail, force=True)


# Single shared instance — see StateManager docstring for why this
# isn't per-session.
state_manager = StateManager()
