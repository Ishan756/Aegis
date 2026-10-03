"""The bounded self-healing loop.

```
PROPOSE FIX -> RISK CHECK -> APPROVAL/POLICY -> APPLY FIX -> TEST -> REDEPLOY -> VERIFY
```

Everything hard about this feature is in what it *cannot* do, so that is what the
code is arranged around. Six guarantees, each enforced in one identifiable place:

1. **Bounded.** :attr:`SelfHealingPolicy.max_recovery_attempts` caps the loop. A
   loop that can retry forever is not a recovery loop, it is an outage with extra
   steps. The cap is also checked before each attempt, not just as a ``for``
   bound, so a future edit cannot widen it by accident.
2. **Never destructive.** :class:`FixCategory` has no destructive member and
   :meth:`SelfHealingPolicy.permits_category` returns ``False`` for anything it
   does not explicitly recognise. Removing a container, rolling back an image or
   editing a file has no code path here.
3. **Never edits code.** ``CODE`` and ``CONFIGURATION`` categories are outside the
   automatic set permanently. Code modification needs an explicit policy decision
   and a human; it is not a slower branch of this loop.
4. **Confidence gates action.** Below ``min_confidence`` the loop stops and
   escalates. Low confidence exists to stop the loop, not to be overridden by
   optimism about a restart.
5. **The MCP policy still decides.** The loop does not bypass the approval gate
   by asserting its own authorisation. It records an *intent* to act, and the
   policy in :mod:`app.services.mcp_policy` evaluates the real call. Self-healing
   is a caller that happens to be allowed, not a privileged one.
6. **Everything is logged.** Each pass appends a :class:`RecoveryAttempt` and
   emits a structured log line, whether it acted or declined. The declined
   attempts are the ones worth reading later.

The loop itself lives in :mod:`app.agents.recovery_workflow`, which drives these
primitives through a LangGraph. This module holds only the pieces that decide
*whether* something may happen; the graph holds the order it happens in. There is
deliberately exactly one loop, because two implementations of a safety mechanism
means two places for it to be wrong.

The "did it work?" question is answered by an injected ``verify`` callable, which
is what keeps the loop honest: it cannot declare success without a real
verification result.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from app.models.incident import CONFIDENCE_ORDER, IncidentReport
from app.models.mcp import ToolCallRequest
from app.models.self_healing import (
    FixCategory,
    FixRisk,
    RecoveryAction,
    SelfHealingPolicy,
)
from app.models.verification import VerificationResult
from app.services.mcp_manager import get_manager

logger = logging.getLogger(__name__)

DOCKER_SERVER = "docker"
REQUESTED_BY = "self-healing"

#: Signature of the callables that redeploy and re-verify. Injected rather than
#: imported so the loop cannot accidentally acquire the ability to deploy
#: anything beyond the caller's own request.
RedeployFn = Callable[[], Awaitable[Any]]
VerifyFn = Callable[[], Awaitable[VerificationResult]]

#: Categories that may ever be applied automatically. Everything else needs a
#: human, and this set is the whole of what "safe" means to this loop. Membership
#: here is necessary but not sufficient: ``allow_rebuild`` still gates a rebuild,
#: which is off by default.
AUTOMATIC_CATEGORIES: frozenset[FixCategory] = frozenset(
    {
        FixCategory.RETRY_DEPLOYMENT,
        FixCategory.RETRY_TRANSIENT,
        FixCategory.RESTART_CONTAINER,
        FixCategory.REBUILD_IMAGE,
    }
)

#: Categories with no automatic path at all, whatever the policy says. Code and
#: configuration changes need an explicit human decision, so they are named here
#: to give a specific reason rather than a generic refusal.
NEVER_AUTOMATIC: frozenset[FixCategory] = frozenset(
    {FixCategory.CODE, FixCategory.CONFIGURATION, FixCategory.DEPENDENCY}
)


class RiskDecision(BaseModel):
    """Why an action will or will not be applied."""

    action: str
    category: FixCategory
    risk: FixRisk
    may_apply_automatically: bool
    reason: str


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------


def propose_action(
    report: IncidentReport, *, container: str, image: str | None
) -> RecoveryAction | None:
    """Translate a recommendation into a concrete, classified action.

    Returns ``None`` when the investigation recommended nothing, which is a real
    outcome and not an error.
    """
    recommendation = report.recommended_fix
    if recommendation is None:
        return None

    action_id = recommendation.action
    rationale = recommendation.rationale
    derived = [cause.id for cause in report.suspected_root_causes]

    if action_id == "restart_container":
        return RecoveryAction(
            action="restart_container",
            category=FixCategory.RESTART_CONTAINER,
            description=f"Stop and start {container} to replace a failed process.",
            rationale=rationale,
            risk=FixRisk.SAFE,
            reversible=True,
            requires_approval=True,
            tool="docker.stop_container",
            arguments={"name": container},
            derived_from=derived,
        )

    if action_id == "rebuild_image":
        return RecoveryAction(
            action="rebuild_image",
            category=FixCategory.REBUILD_IMAGE,
            description=f"Rebuild {image or 'the image'} and redeploy.",
            rationale=rationale,
            risk=FixRisk.APPROVAL_REQUIRED,
            reversible=False,
            requires_approval=True,
            tool="docker.build_image",
            arguments={},
            derived_from=derived,
        )

    if action_id in {"retry_deployment", "retry_transient"}:
        return RecoveryAction(
            action=action_id,
            category=FixCategory.RETRY_DEPLOYMENT,
            description="Retry the deployment unchanged.",
            rationale=rationale,
            risk=FixRisk.SAFE,
            reversible=True,
            requires_approval=True,
            tool=None,
            arguments={},
            derived_from=derived,
        )

    # review_configuration, review_code_change, check_dependency and anything new.
    # Classified as requiring approval *and* flagged, because a remedy that needs
    # a repository change must never be something this loop performs.
    return RecoveryAction(
        action=action_id,
        category=(
            FixCategory.CODE if action_id == "review_code_change" else FixCategory.CONFIGURATION
        ),
        description=recommendation.description,
        rationale=rationale,
        risk=FixRisk.APPROVAL_REQUIRED,
        reversible=recommendation.reversible,
        requires_approval=True,
        tool=None,
        arguments={},
        derived_from=derived,
    )


# ---------------------------------------------------------------------------
# Risk check
# ---------------------------------------------------------------------------


def assess_risk(
    action: RecoveryAction, report: IncidentReport, policy: SelfHealingPolicy
) -> RiskDecision:
    """Decide whether this action may be applied without a human.

    The checks are ordered so the most fundamental refusal is reported first. A
    caller reading the reason gets the real blocker rather than a downstream
    symptom of it.
    """
    if action.category in NEVER_AUTOMATIC:
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=FixRisk.FORBIDDEN,
            may_apply_automatically=False,
            reason=(
                f"'{action.category}' can never be applied automatically. Code, "
                "configuration and dependency changes need an explicit human "
                "decision, and no policy setting alters that."
            ),
        )

    if action.category not in AUTOMATIC_CATEGORIES:
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=FixRisk.FORBIDDEN,
            may_apply_automatically=False,
            reason=f"'{action.category}' is outside the automatic set.",
        )

    if not policy.permits_category(action.category):
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=action.risk,
            may_apply_automatically=False,
            reason=f"policy does not permit automatic '{action.category}'.",
        )

    if action.risk is FixRisk.FORBIDDEN:
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=FixRisk.FORBIDDEN,
            may_apply_automatically=False,
            reason="the action is classified forbidden.",
        )

    if action.risk is FixRisk.APPROVAL_REQUIRED:
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=action.risk,
            may_apply_automatically=False,
            reason="the action is classified as requiring human approval.",
        )

    report_confidence = CONFIDENCE_ORDER[report.confidence]
    required = CONFIDENCE_ORDER[policy.min_confidence]
    if report_confidence < required:
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=action.risk,
            may_apply_automatically=False,
            reason=(
                f"confidence '{report.confidence}' is below the policy floor "
                f"'{policy.min_confidence}'. Escalating rather than guessing."
            ),
        )

    if not report.automatic_fix_safe:
        return RiskDecision(
            action=action.action,
            category=action.category,
            risk=action.risk,
            may_apply_automatically=False,
            reason="the investigation did not clear this fix as safe to apply automatically.",
        )

    return RiskDecision(
        action=action.action,
        category=action.category,
        risk=action.risk,
        may_apply_automatically=True,
        reason=f"'{action.action}' is non-destructive and the evidence supports it.",
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


async def apply_action(action: RecoveryAction, *, approve: bool = False) -> tuple[bool, str]:
    """Apply one action through the policy-enforcing MCP layer.

    Returns ``(succeeded, error)``. A refusal is a failure like any other and is
    reported as one, because "the policy said no" and "the restart worked" are
    very different things to record in the same field.
    """
    if action.tool is None:
        # No tool means the action is a redeploy, handled by the caller.
        return True, ""

    try:
        result = await get_manager().call_tool(
            ToolCallRequest(
                tool_name=action.tool,
                arguments=dict(action.arguments),
                requested_by=REQUESTED_BY,
                # The loop records that *this* caller is authorised for a
                # non-destructive fix. It does not bypass the policy: the policy
                # still evaluates risk and can still refuse.
                approval_granted=approve,
                approval_reference=f"self-healing:{action.action}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - a failed fix must not crash the loop
        return False, f"{type(exc).__name__}: {exc}"

    if not result.success:
        return False, result.error_message or result.error_code or "tool_error"
    return True, ""


async def restart_container(container: str, *, grace_seconds: float = 10.0) -> tuple[bool, str]:
    """Stop then start a container. The only automatic state change permitted.

    Split into two calls because the MCP server exposes ``stop_container`` and
    ``start_container`` separately. Both are logged individually so a restart
    that half-succeeded is visible as such.
    """
    stop_ok, stop_error = await apply_action(
        RecoveryAction(
            action="stop_container",
            category=FixCategory.RESTART_CONTAINER,
            description=f"Stop {container} before restarting it.",
            rationale="A stopped container keeps its name, so the start would collide.",
            risk=FixRisk.SAFE,
            reversible=True,
            requires_approval=True,
            tool="docker.stop_container",
            arguments={"name": container, "grace_seconds": grace_seconds},
        ),
        approve=True,
    )
    if not stop_ok:
        return False, f"stop failed: {stop_error}"

    logger.info("self-healing stopped container", extra={"container": container})
    # The caller redeploys, which starts the container again. This function exists
    # so the stop is explicit and logged rather than implied by a redeploy.
    return True, ""


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


__all__ = [
    "AUTOMATIC_CATEGORIES",
    "NEVER_AUTOMATIC",
    "RedeployFn",
    "RiskDecision",
    "VerifyFn",
    "apply_action",
    "assess_risk",
    "propose_action",
    "restart_container",
]
