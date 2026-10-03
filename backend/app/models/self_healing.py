"""Self-healing policy models.

This module describes **what may be done without a human**, and nothing else. The
answer is deliberately narrow, and the shape of the answer matters as much as its
content: an action carries its own risk classification, so classifying an action
cannot be forgotten at the call site.

The ordering in :class:`FixRisk` is meaningful, and the rules are:

- ``SAFE`` — may be applied automatically, but only if confidence is high enough
  and the retry budget allows. Restarting a container and retrying a deployment
  live here.
- ``APPROVAL_REQUIRED`` — a human must approve. Rebuilding an image is here: it
  re-executes the Dockerfile's ``RUN`` steps against untrusted input and can take
  minutes, which is not something to trigger unattended on a guess.
- ``FORBIDDEN`` — never automatic, by this loop, under any configuration.
  Destroying state is permanently off the table. There is no setting that turns
  it on, because a flag that disables the safety guarantee is worse than no flag.

That last point is why :class:`SelfHealingPolicy` has no ``allow_destructive``
field. If you need one, you need a different process.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.incident import IncidentReport


class FixRisk(StrEnum):
    """How much danger a proposed remedy carries."""

    SAFE = "safe"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


#: Ordered worst-last, so ``max()`` over a set of risks finds the most dangerous.
RISK_ORDER: dict[FixRisk, int] = {
    FixRisk.SAFE: 0,
    FixRisk.APPROVAL_REQUIRED: 1,
    FixRisk.FORBIDDEN: 2,
}


class FixCategory(StrEnum):
    """What kind of remedy was proposed, independent of its risk."""

    RETRY_DEPLOYMENT = "retry_deployment"
    RETRY_TRANSIENT = "retry_transient"
    RESTART_CONTAINER = "restart_container"
    REBUILD_IMAGE = "rebuild_image"
    CONFIGURATION = "configuration"
    CODE = "code"
    DEPENDENCY = "dependency"


class RecoveryAction(BaseModel):
    """A concrete, applicable remedy."""

    #: Machine-readable action id, e.g. ``restart_container``.
    action: str
    category: FixCategory
    description: str
    rationale: str
    risk: FixRisk
    reversible: bool = False
    requires_approval: bool = True
    tool: str | None = Field(default=None, description="MCP tool this calls.")
    arguments: dict[str, object] = Field(default_factory=dict)
    #: The incident cause ids that motivated this. An action with no cause behind
    #: it is not a fix, it is a coincidence.
    derived_from: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _approval_matches_risk(self) -> RecoveryAction:
        """A forbidden action can never claim it needs only approval.

        Without this, promoting a fix to ``approval_required`` would read as a
        weaker, more approvable thing, and the distinction would quietly invert.
        """
        if self.risk is FixRisk.FORBIDDEN and self.requires_approval is not True:
            raise ValueError(
                f"action '{self.action}' is forbidden but claims approval is "
                "sufficient; forbidden actions cannot be approved into existence"
            )
        return self


class SelfHealingPolicy(BaseModel):
    """The envelope. Every automatic action is checked against this.

    Defaults are the safe end of every axis: off, bounded, and demanding
    evidence. :func:`is_active` is what the loop consults, so a policy can be
    constructed and inspected without granting anything.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch. False means the loop diagnoses and stops, which is the "
            "correct behaviour for a system nobody has watched recover yet."
        ),
    )

    max_recovery_attempts: int = Field(
        default=2,
        ge=0,
        le=10,
        description=(
            "Total automatic attempts per incident, including the first. Bounded "
            "because a loop that can retry forever is indistinguishable from an "
            "outage."
        ),
    )

    min_confidence: Literal["low", "medium", "high"] = Field(
        default="medium",
        description=(
            "Below this, the loop stops and escalates. Default 'medium' means a "
            "single strong signature is not enough to act on unattended."
        ),
    )

    allow_restart: bool = Field(
        default=True,
        description=(
            "Permit automatic restart of a failed container. Restart is "
            "non-destructive: it replaces a process that is already not working, "
            "and it is logged. It is not free, so it is separately switchable."
        ),
    )

    allow_rebuild: bool = Field(
        default=False,
        description=(
            "Permit automatic image rebuild. Off by default: a rebuild executes the "
            "Dockerfile's RUN steps against untrusted input and can take minutes. "
            "Enable it once you trust the repositories being deployed."
        ),
    )

    allow_retry: bool = Field(
        default=True, description="Permit retrying the deployment or a transient operation."
    )

    @property
    def is_active(self) -> bool:
        """True when this policy authorises any automatic action at all."""
        return self.enabled and self.max_recovery_attempts > 0

    def permits_category(self, category: FixCategory) -> bool:
        """Whether a category is in scope for automatic application.

        Note what is absent: no branch grants ``CODE`` or ``CONFIGURATION``. Code
        modification is not a slow path here, it is not a path at all.
        """
        if category is FixCategory.RETRY_DEPLOYMENT or category is FixCategory.RETRY_TRANSIENT:
            return self.allow_retry
        if category is FixCategory.RESTART_CONTAINER:
            return self.allow_restart
        if category is FixCategory.REBUILD_IMAGE:
            return self.allow_rebuild
        return False


class EscalationReason(StrEnum):
    """Why the loop stopped instead of acting."""

    #: Self-healing is switched off.
    DISABLED = "disabled"
    #: The retry budget is spent.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: Confidence below the configured floor.
    LOW_CONFIDENCE = "low_confidence"
    #: The proposed fix needs a human.
    APPROVAL_REQUIRED = "approval_required"
    #: The proposed fix is permanently off-limits to automation.
    FORBIDDEN_FIX = "forbidden_fix"
    #: The investigation could not establish a cause.
    NO_ROOT_CAUSE = "no_root_cause"
    #: Nothing was proposed.
    NO_FIX_PROPOSED = "no_fix_proposed"


class RecoveryAttempt(BaseModel):
    """One pass through the loop, fully recorded.

    Every automatic action leaves one of these. ``applied`` is False for actions
    that were considered and declined, which is the more interesting half of the
    history and would otherwise be invisible.
    """

    index: int = Field(ge=1, description="1-based attempt number.")
    action: str
    category: FixCategory
    risk: FixRisk
    description: str
    applied: bool
    approved: bool = False
    succeeded: bool = False
    error: str | None = None
    evidence: list[str] = Field(default_factory=list)
    #: The verification status after redeploying, when the attempt got that far.
    verification_status: str | None = None

    def summary(self) -> str:
        state = "applied" if self.applied else "not applied"
        outcome = "succeeded" if self.succeeded else "did not recover"
        return f"#{self.index} {self.action} ({self.risk}) {state}, {outcome}"


class RecoveryOutcome(BaseModel):
    """The result of the whole loop.

    ``recovered`` is the only field that means the deployment works again.
    Everything else records how it was decided, so a reviewer can tell a genuine
    recovery from an exhausted budget that happened to look similar.
    """

    recovered: bool = False
    attempts: list[RecoveryAttempt] = Field(default_factory=list)
    attempts_used: int = Field(default=0, ge=0)
    budget: int = Field(default=0, ge=0)
    escalated: bool = False
    escalation_reason: EscalationReason | None = None
    #: What a human should do now.
    next_action: str = ""
    #: The verification result at the end, successful or not.
    final_verification: object | None = None
    #: The investigation behind this outcome. Carried rather than discarded: an
    #: outcome with no report behind it cannot be reviewed, and the evidence is
    #: the only part of this feature a human can check.
    incident: IncidentReport | None = None

    @property
    def exhausted(self) -> bool:
        """True when the budget ran out before anything worked."""
        return self.escalation_reason is EscalationReason.BUDGET_EXHAUSTED

    def summary(self) -> str:
        verdict = "recovered" if self.recovered else "not recovered"
        detail = f"{self.attempts_used}/{self.budget} attempts"
        if self.escalation_reason:
            detail += f", escalated: {self.escalation_reason}"
        return f"{verdict} ({detail})"


__all__ = [
    "RISK_ORDER",
    "EscalationReason",
    "FixCategory",
    "FixRisk",
    "RecoveryAction",
    "RecoveryAttempt",
    "RecoveryOutcome",
    "SelfHealingPolicy",
]
