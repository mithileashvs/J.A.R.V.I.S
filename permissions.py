"""
JARVIS permission system.

Classifies every tool into SAFE / CONFIRM / BLOCKED and manages the
confirm-before-execute flow for anything that isn't SAFE. This is the
piece that was previously entirely missing: send_email and
open_application executed immediately with zero user confirmation.

Design:
- SAFE tools run immediately, no user interaction needed.
- CONFIRM tools go through request_confirmation() -> a pending entry is
  stored and broadcast to the UI; execution only proceeds once
  resolve_confirmation() is called with approved=True, driven by the
  user tapping Allow/Deny in the frontend.
- BLOCKED tools never execute automatically under any circumstance —
  there is deliberately no code path that runs a BLOCKED tool, not
  even with confirmation. Anything genuinely dangerous (arbitrary
  shell execution, deleting files outside the project, etc.) belongs
  here, not in CONFIRM.

This module does not know how to actually run a tool — tool_registry.py
owns that. permissions.py only decides whether a given tool is allowed
to run right now.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("jarvis-permissions")

BroadcastFn = Callable[[dict], Awaitable[None]]


class PermissionLevel(str, Enum):
    SAFE = "SAFE"                # runs automatically, no confirmation
    CONFIRM = "CONFIRM"          # requires explicit user Allow before running
    BLOCKED = "BLOCKED"          # never runs, ever — no confirmation unlocks this


@dataclass
class PendingConfirmation:
    id: str
    tool_name: str
    args: dict
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    resolved: bool = False
    approved: Optional[bool] = None


class PermissionManager:
    """
    Holds in-memory pending confirmations. Not persisted — if the
    backend restarts mid-confirmation, the pending request is gone and
    the user would need to re-issue the command. That's the right
    default: a stale "Allow?" prompt surviving a restart and getting
    approved days later by accident is worse than making the user ask
    again.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}

    async def request_confirmation(
        self,
        tool_name: str,
        args: dict,
        reason: str,
        broadcast: Optional[BroadcastFn] = None,
    ) -> PendingConfirmation:
        confirmation = PendingConfirmation(
            id=str(uuid.uuid4()),
            tool_name=tool_name,
            args=args,
            reason=reason,
        )
        self._pending[confirmation.id] = confirmation

        logger.info(f"[permissions] Confirmation requested: {tool_name} ({confirmation.id})")

        if broadcast is not None:
            try:
                await broadcast({
                    "type": "confirmation_request",
                    "id": confirmation.id,
                    "tool": tool_name,
                    "args": args,
                    "reason": reason,
                    "timestamp": confirmation.created_at.isoformat(),
                })
            except Exception as e:
                logger.warning(f"[permissions] Broadcast failed: {e}")

        return confirmation

    def resolve_confirmation(self, confirmation_id: str, approved: bool) -> Optional[PendingConfirmation]:
        confirmation = self._pending.get(confirmation_id)
        if confirmation is None:
            logger.warning(f"[permissions] Unknown confirmation id: {confirmation_id}")
            return None
        if confirmation.resolved:
            # Bug fix: a confirmation must only ever be resolved once. Without
            # this guard, POSTing /confirmations/{id} a second time (e.g. Deny
            # followed by a replayed Approve) would re-run resolve_confirmation
            # and main.py's endpoint would then execute the tool despite the
            # original Deny — silently overturning the user's decision instead
            # of requiring a fresh confirmation request. Treat a second
            # resolution attempt the same as an unknown id.
            logger.warning(
                f"[permissions] Confirmation {confirmation_id} was already resolved "
                f"({'approved' if confirmation.approved else 'denied'}); ignoring replay."
            )
            return None
        confirmation.resolved = True
        confirmation.approved = approved
        logger.info(
            f"[permissions] Confirmation {confirmation_id} resolved: "
            f"{'APPROVED' if approved else 'DENIED'}"
        )
        return confirmation

    def get_pending(self, confirmation_id: str) -> Optional[PendingConfirmation]:
        return self._pending.get(confirmation_id)

    def list_pending(self) -> list[PendingConfirmation]:
        return [c for c in self._pending.values() if not c.resolved]

    def clear_resolved(self, older_than_seconds: int = 300) -> None:
        """Housekeeping — drop resolved/stale entries so this dict doesn't grow forever."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stale_ids = [
            cid for cid, c in self._pending.items()
            if c.resolved or (now - c.created_at).total_seconds() > older_than_seconds
        ]
        for cid in stale_ids:
            del self._pending[cid]


# Single shared instance, mirroring state_manager's pattern in state.py.
permission_manager = PermissionManager()
