"""
JARVIS confidence system (Phase 5, Section 12/13).

Turns "how sure am I about what the user wants" into one of three
levels — HIGH / MEDIUM / LOW — and a recommended next action. This
module makes a *recommendation only*; it has no ability to run
anything itself and never touches permissions.py or tool_registry.py
directly. The actual execute/confirm/block decision for a given tool
call still goes through the existing Permission Manager exactly as it
does today — this module cannot be used to bypass that, by design
(Section 12: "Never allow the LLM to bypass the permission system
because it is highly confident").

What this module actually decides: whether JARVIS should (a) just go
ahead and let the normal permission flow run, (b) ask a one-line
"are you sure you meant X" confirmation before even attempting the
action, or (c) stop and ask a clarifying question because it doesn't
know what the user wants yet. That's a conversational decision, not a
security one — security is still 100% permissions.py's job.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RecommendedAction(str, Enum):
    PROCEED = "PROCEED"                    # let the normal permission flow run
    CONFIRM_INTENT = "CONFIRM_INTENT"      # ask "did you mean X?" before attempting
    CLARIFY = "CLARIFY"                    # stop, ask what they actually want


@dataclass
class ConfidenceDecision:
    confidence: Confidence
    action: RecommendedAction
    reason: str


# Below this, JARVIS shouldn't guess at all — matches intent_router.py's
# own fallback-to-GENERAL_CHAT behavior on classification failure
# (confidence 0.0), so LOW here lines up with "the router itself wasn't
# sure" rather than introducing a second, inconsistent threshold.
_LOW_THRESHOLD = 0.45
# Above this, treat the classification as solid enough to act on
# without double-checking, unless the action itself is consequential.
_HIGH_THRESHOLD = 0.75


def evaluate(
    intent_confidence: float,
    *,
    is_consequential: bool,
    reference_resolution_confidence: Optional[float] = None,
) -> ConfidenceDecision:
    """
    Combine the intent router's own confidence with (optionally) how
    sure the reference resolver was about resolving a contextual
    command ("open it", "the second one") into something concrete.

    is_consequential marks whether the action this intent would lead
    to has real-world effect if wrong (running a command, modifying a
    file, deleting something) vs. purely informational (answering a
    question, explaining code). Section 12's HIGH/MEDIUM/LOW table is
    defined in terms of "the action" being consequential or not, so
    that's a required input rather than something this module infers.
    """
    combined = intent_confidence
    if reference_resolution_confidence is not None:
        # A reference that couldn't be resolved caps the combined
        # confidence — being sure about the *verb* ("open") doesn't
        # help if we're not sure what "it" refers to.
        combined = min(combined, reference_resolution_confidence)

    if combined < _LOW_THRESHOLD:
        return ConfidenceDecision(
            confidence=Confidence.LOW,
            action=RecommendedAction.CLARIFY,
            reason=f"Combined confidence {combined:.2f} is below the clarify threshold.",
        )

    if combined < _HIGH_THRESHOLD or is_consequential:
        # MEDIUM confidence always asks for confirmation on anything
        # consequential too — a HIGH-confidence guess about a
        # destructive action still gets a "did you mean X?" per
        # Section 12 ("MEDIUM: Ask confirmation if the action is
        # consequential"), read together with Section 13's stricter
        # rule for destructive actions.
        level = Confidence.HIGH if combined >= _HIGH_THRESHOLD else Confidence.MEDIUM
        reason = (
            "Consequential action — confirming intent before proceeding."
            if is_consequential and level == Confidence.HIGH
            else f"Combined confidence {combined:.2f} is in the confirm range."
        )
        return ConfidenceDecision(
            confidence=level,
            action=RecommendedAction.CONFIRM_INTENT,
            reason=reason,
        )

    return ConfidenceDecision(
        confidence=Confidence.HIGH,
        action=RecommendedAction.PROCEED,
        reason=f"Combined confidence {combined:.2f} is high and the action is not consequential.",
    )
