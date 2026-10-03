"""The recovery graph.

```
DEBUG -> ROOT CAUSE -> FIX RECOMMENDATION -> RISK CHECK -> APPROVAL -> APPLY FIX
                                                                    |
                                       TEST / REDEPLOY / VERIFY <----+
                                            |              |
                                       SUCCESS          budget spent
                                            |              |
                                           END          ESCALATE
```

The diagram is the point of the module. Every arrow corresponds to a node whose
refusal is a normal, tested outcome rather than an exception, because the loop
that matters is the one that *stops*:

```
RISK CHECK --forbidden/low-confidence/needs-approval--> ESCALATE (no action taken)
VERIFY --SUCCESS--> END
VERIFY --failure--> FIX RECOMMENDATION, but only while attempts remain
```

`attempt` is part of the state and is incremented at :func:`apply_fix`, so the
loop bound lives in the graph rather than in a caller that might forget it.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.investigation import investigate_failure
from app.agents.self_healing import (
    RedeployFn,
    RiskDecision,
    VerifyFn,
    apply_action,
    assess_risk,
    propose_action,
    restart_container,
)
from app.models.incident import CONFIDENCE_ORDER, IncidentReport
from app.models.self_healing import (
    EscalationReason,
    FixCategory,
    FixRisk,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryOutcome,
    SelfHealingPolicy,
)
from app.models.verification import VerificationResult

logger = logging.getLogger(__name__)


class RecoveryState(TypedDict, total=False):
    """State for the recovery graph.

    Declared rather than left as ``dict[str, Any]`` so the keys the loop depends
    on -- especially ``attempt`` -- are part of the contract.
    """

    # Input
    verification: VerificationResult | None
    stop_reason: str | None
    stop_task_id: str | None
    container: str | None
    image: str | None
    repository_path: str | None
    repository: str | None
    policy: SelfHealingPolicy
    human_approved: bool

    # Derived
    incident: IncidentReport
    action: RecoveryAction | None
    decision: RiskDecision | None
    attempts: list[RecoveryAttempt]
    attempt: int
    escalation: EscalationReason | None
    next_action: str
    outcome: RecoveryOutcome
    recovery_succeeded: bool

    last_verification: VerificationResult | None

    # Callables supplied by the caller. Carried through state so this module
    # never has to import the deployment workflow to redeploy.
    _verify_fn: Any
    _redeploy_fn: Any
    apply_error: str | None


# ---------------------------------------------------------------------------
# DEBUG / ROOT CAUSE / FIX RECOMMENDATION
# ---------------------------------------------------------------------------


async def debug_node(state: RecoveryState) -> dict[str, Any]:
    """DEBUG: collect evidence. Read-only, by construction and by annotation."""
    report = await investigate_failure(
        state.get("verification"),
        container=state.get("container"),
        image=state.get("image"),
        repository_path=state.get("repository_path"),
        repository=state.get("repository"),
        stop_reason=state.get("stop_reason"),
        stop_task_id=state.get("stop_task_id"),
    )
    logger.info(
        "recovery graph debug complete",
        extra={
            "incident_id": report.incident_id,
            "observations": len(report.evidence),
            "confidence": report.confidence,
            "inconclusive": report.inconclusive,
        },
    )
    # Deliberately does NOT reset `attempt` or `attempts`. This node runs again on
    # every retry pass, and zeroing the counter here made the budget inexhaustible:
    # the loop restarted its own allowance each round and never terminated.
    #
    # `escalation` *is* cleared, so a reason from a previous round does not
    # suppress the action chosen in this one.
    return {
        "incident": report,
        "attempts": list(state.get("attempts", [])),
        "attempt": int(state.get("attempt", 0)),
        "escalation": None,
    }


async def root_cause_node(state: RecoveryState) -> dict[str, Any]:
    """ROOT CAUSE: the incident report already carries the derived causes.

    Kept as a node rather than folded into DEBUG so the graph reads the way the
    failure is actually reasoned about, and so the confidence gate has a place to
    sit between cause and remedy.
    """
    report = state["incident"]
    if not report.suspected_root_causes:
        return {
            "escalation": EscalationReason.NO_ROOT_CAUSE,
            "next_action": (
                "No root cause could be established from the available evidence. "
                "A human needs to investigate; nothing will be changed automatically."
            ),
        }

    primary = report.primary_cause
    return {
        "next_action": (
            f"Working hypothesis: {primary.cause} "
            f"(component: {primary.component}, confidence: {primary.confidence})."
            if primary
            else ""
        )
    }


async def fix_recommendation_node(state: RecoveryState) -> dict[str, Any]:
    """FIX RECOMMENDATION: turn the hypothesis into one classified action."""
    report = state["incident"]
    action = propose_action(
        report, container=state.get("container") or "the container", image=state.get("image")
    )
    if action is None:
        return {
            "action": None,
            "escalation": EscalationReason.NO_FIX_PROPOSED,
            "next_action": "The investigation proposed no actionable fix.",
        }
    return {"action": action}


# ---------------------------------------------------------------------------
# RISK CHECK / APPROVAL
# ---------------------------------------------------------------------------


async def risk_check_node(state: RecoveryState) -> dict[str, Any]:
    """RISK CHECK: the last place an automatic action can be refused."""
    report = state["incident"]
    action = state.get("action")
    policy = state["policy"]

    if action is None:
        return {"decision": None}

    decision = assess_risk(action, report, policy)
    logger.info(
        "recovery graph risk check",
        extra={
            "action": action.action,
            "risk": str(decision.risk),
            "may_apply_automatically": decision.may_apply_automatically,
            "reason": decision.reason,
        },
    )
    return {"decision": decision}


def route_after_risk_check(state: RecoveryState) -> str:
    """APPROVAL/POLICY: act, or stop.

    ``human_approved`` is the only thing that can promote an
    ``approval_required`` action, and it comes from the caller. Nothing in this
    graph can set it, which is what stops an agent approving its own action.

    The reason for a refusal is derived in ``escalate_node`` from the decision
    left in state, rather than returned alongside the route: this LangGraph
    version rejects a state update from a conditional edge.
    """
    decision = state.get("decision")
    if decision is None:
        return "escalate"

    if decision.may_apply_automatically:
        return "apply"

    # A forbidden action stays forbidden. Human approval buys an
    # `approval_required` action, not a destructive one.
    if decision.risk is FixRisk.FORBIDDEN:
        return "escalate"

    if state.get("human_approved"):
        logger.info(
            "recovery graph applying human-approved action",
            extra={"action": decision.action, "risk": str(decision.risk)},
        )
        return "apply"

    return "escalate"


# ---------------------------------------------------------------------------
# APPLY FIX / TEST / REDEPLOY / VERIFY
# ---------------------------------------------------------------------------


async def apply_fix_node(state: RecoveryState) -> dict[str, Any]:
    """APPLY FIX. Increments the attempt counter *before* acting.

    Counting first means an action that raises still consumed budget, so a tool
    that fails in an unexpected way cannot produce an unbounded loop.
    """
    action = state["action"]
    decision = state["decision"]
    policy = state["policy"]
    attempt = int(state.get("attempt", 0)) + 1

    record = RecoveryAttempt(
        index=attempt,
        action=action.action,
        category=action.category,
        risk=decision.risk if decision else action.risk,
        description=action.description,
        applied=True,
        approved=True,
        evidence=[cause.id for cause in state["incident"].suspected_root_causes],
    )

    logger.info(
        "recovery graph applying fix",
        extra={
            "action": action.action,
            "category": str(action.category),
            "attempt": attempt,
            "budget": policy.max_recovery_attempts,
            "human_approved": bool(state.get("human_approved")),
        },
    )

    error: str | None = None
    if action.category is FixCategory.RESTART_CONTAINER:
        # Checked before the stop, not after. `restart_container` is really a stop;
        # without a redeploy that starts the container again this would take a
        # working service down and leave it down. Refusing is the only safe option.
        if state.get("_redeploy_fn") is None:
            error = (
                "restart refused: this caller supplied no way to start the container "
                "again. Stopping it here would leave it down, so nothing was changed."
            )
            logger.info(
                "recovery graph refused restart without a redeploy path",
                extra={"action": action.action, "attempt": attempt},
            )
        else:
            ok, stop_error = await restart_container(state.get("container") or "")
            if not ok:
                error = f"restart failed: {stop_error}"

    calls_its_own_tool = (
        action.tool is not None and action.category is not FixCategory.RESTART_CONTAINER
    )
    if error is None and calls_its_own_tool:
        ok, tool_error = await apply_action(action, approve=True)
        if not ok:
            error = tool_error

    if error:
        record.error = error
        # An action that raised changed nothing, so it must not read as applied.
        # `approved` still records that a human's approval was consumed by it.
        record.applied = False
        logger.warning("recovery graph fix failed", extra={"action": action.action, "error": error})

    attempts = list(state.get("attempts", []))
    attempts.append(record)
    return {"attempt": attempt, "attempts": attempts, "apply_error": error}


def route_after_apply(state: RecoveryState) -> str:
    """A fix that errored skips redeploy and verification entirely.

    Redeploying after a failed restart is worse than doing nothing: the stop may
    have succeeded, leaving the container down, and a verify would then measure a
    container nobody started.
    """
    policy = state["policy"]
    exhausted = int(state.get("attempt", 0)) >= policy.max_recovery_attempts
    if state.get("apply_error"):
        return "escalate" if exhausted else "debug"
    return "escalate" if exhausted else "redeploy"


async def redeploy_node(state: RecoveryState) -> dict[str, Any]:
    """REDEPLOY: start the workload again before it can be tested.

    Skipping this would verify a container the restart deliberately stopped, and
    report the outage as a recovery.
    """
    redeploy = state.get("_redeploy_fn")
    if not callable(redeploy):
        return {}

    try:
        await redeploy()
    except Exception as exc:  # noqa: BLE001
        logger.warning("recovery graph redeploy raised", extra={"error": str(exc)})
        attempts = list(state.get("attempts", []))
        if attempts:
            attempts[-1].error = f"redeploy failed: {type(exc).__name__}: {exc}"
        return {"attempts": attempts, "apply_error": "redeploy failed"}

    return {}


async def verify_node(state: RecoveryState) -> dict[str, Any]:
    """TEST / VERIFY: the only node allowed to declare success.

    Without this the loop would trust that the action worked, which is exactly
    the assumption that turns a recovery loop into a way to make an outage
    permanent.
    """
    verify = state.get("_verify_fn")
    result: VerificationResult | None = None

    if callable(verify):
        try:
            result = await verify()
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovery graph verification raised", extra={"error": str(exc)})

    attempts = list(state.get("attempts", []))
    succeeded = False
    if attempts:
        attempts[-1].verification_status = result.status if result else None
        # A green verification is only a recovery if the fix actually applied. If
        # `apply_error` is set the container may not even be running, and calling
        # that a recovery is how a loop declares success over a broken system.
        succeeded = bool(result and result.status == "SUCCESS") and not state.get("apply_error")
        attempts[-1].succeeded = succeeded

    return {
        "attempts": attempts,
        "last_verification": result,
        "recovery_succeeded": succeeded,
    }


def route_after_verify(state: RecoveryState) -> str:
    """SUCCESS ends the run. Failure loops only while budget remains."""
    if state.get("recovery_succeeded"):
        return "end"

    policy = state["policy"]
    if int(state.get("attempt", 0)) >= policy.max_recovery_attempts:
        return "escalate"

    # Re-investigate: the container's state has changed, so yesterday's evidence
    # may no longer describe it. Skipping this is how a loop ends up restarting a
    # container forever on a stale diagnosis.
    return "debug"


async def escalate_node(state: RecoveryState) -> dict[str, Any]:
    """Stop. Change nothing further. Say what a human should do."""
    decision = state.get("decision")

    # Distinguish "the policy stopped this" from "we ran out of retries". They
    # demand different responses, and reporting the latter when it was the former
    # points an operator at a retry limit that raising would not help.
    incident = state.get("incident")
    policy = state["policy"]

    attempts = list(state.get("attempts", []))

    # `escalate` is reached by two very different routes, and they need different
    # responses: the policy refused the action (a human can change that) or the
    # loop acted and ran out of budget (a human cannot un-spend a retry). Arriving
    # here is not itself evidence of a refusal -- a decision may be sitting in
    # state from an action that was happily applied.
    reason = state.get("escalation")
    if reason is None:
        if attempts or decision is None:
            reason = EscalationReason.BUDGET_EXHAUSTED
        elif decision.risk is FixRisk.FORBIDDEN:
            reason = EscalationReason.FORBIDDEN_FIX
        elif (
            incident is not None
            and CONFIDENCE_ORDER[incident.confidence] < CONFIDENCE_ORDER[policy.min_confidence]
        ):
            reason = EscalationReason.LOW_CONFIDENCE
        else:
            reason = EscalationReason.APPROVAL_REQUIRED

    # Record the action that was considered and declined. A refusal with no
    # record is indistinguishable from the loop never having run, which makes the
    # history of a system impossible to audit after the fact.
    action = state.get("action")
    if action is not None and decision is not None and not attempts:
        attempts.append(
            RecoveryAttempt(
                index=int(state.get("attempt", 0)) or 1,
                action=action.action,
                category=action.category,
                risk=decision.risk,
                description=decision.reason,
                applied=False,
                approved=False,
                evidence=[cause.id for cause in incident.suspected_root_causes] if incident else [],
            )
        )

    if reason is EscalationReason.NO_ROOT_CAUSE and incident is not None:
        detail = (
            "The investigation found no matching failure signature, so any automated "
            "action would be a guess. Check the full container log."
        )
    elif reason is EscalationReason.NO_FIX_PROPOSED:
        detail = "Nothing actionable was proposed."
    elif reason is EscalationReason.BUDGET_EXHAUSTED:
        last = state.get("last_verification")
        detail = (
            f"Recovery budget of {policy.max_recovery_attempts} attempt(s) is spent "
            f"and verification still reports "
            f"{last.status if last else 'failure'}. This needs a human."
        )
    else:
        detail = incident.next_action if incident else "Human review required."

    logger.warning(
        "recovery graph escalated",
        extra={"reason": str(reason), "attempts": len(attempts)},
    )
    return {"escalation": reason, "next_action": detail, "attempts": attempts}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_recovery_graph() -> CompiledStateGraph:
    """DEBUG -> ROOT CAUSE -> FIX -> RISK CHECK -> APPROVAL -> APPLY -> VERIFY."""
    builder = StateGraph(RecoveryState)

    builder.add_node("debug", debug_node)
    builder.add_node("root_cause", root_cause_node)
    builder.add_node("fix_recommendation", fix_recommendation_node)
    builder.add_node("risk_check", risk_check_node)
    builder.add_node("apply_fix", apply_fix_node)
    builder.add_node("redeploy", redeploy_node)
    builder.add_node("verify", verify_node)
    builder.add_node("escalate", escalate_node)

    builder.add_edge(START, "debug")
    builder.add_edge("debug", "root_cause")

    builder.add_conditional_edges(
        "root_cause",
        lambda state: "escalate" if state.get("escalation") else "fix_recommendation",
        {"fix_recommendation": "fix_recommendation", "escalate": "escalate"},
    )
    builder.add_edge("fix_recommendation", "risk_check")
    builder.add_conditional_edges(
        "risk_check",
        route_after_risk_check,
        {"apply": "apply_fix", "escalate": "escalate"},
    )
    builder.add_conditional_edges(
        "apply_fix",
        route_after_apply,
        {"redeploy": "redeploy", "debug": "debug", "escalate": "escalate"},
    )
    builder.add_conditional_edges(
        "redeploy",
        lambda state: "verify" if not state.get("apply_error") else "escalate",
        {"verify": "verify", "escalate": "escalate"},
    )
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {"debug": "debug", "end": END, "escalate": "escalate"},
    )
    builder.add_edge("escalate", END)

    return builder.compile()


recovery_graph = build_recovery_graph()


async def run_recovery(
    policy: SelfHealingPolicy,
    *,
    verification: VerificationResult | None = None,
    stop_reason: str | None = None,
    stop_task_id: str | None = None,
    container: str | None = None,
    image: str | None = None,
    repository_path: str | None = None,
    repository: str | None = None,
    human_approved: bool = False,
    verify: VerifyFn | None = None,
    redeploy: RedeployFn | None = None,
) -> RecoveryOutcome:
    """Investigate, then attempt bounded recovery. Returns what happened.

    When ``policy`` is disabled the graph still runs the DEBUG through FIX
    stages, because a diagnosis is useful even when acting on it is not
    permitted. It stops at RISK CHECK with a recorded refusal.
    """
    if not policy.is_active:
        report = await investigate_failure(
            verification,
            container=container,
            image=image,
            repository_path=repository_path,
            repository=repository,
            stop_reason=stop_reason,
            stop_task_id=stop_task_id,
        )
        logger.info(
            "self-healing disabled; diagnosis only",
            extra={"incident_id": report.incident_id, "confidence": report.confidence},
        )
        return RecoveryOutcome(
            recovered=False,
            escalated=True,
            escalation_reason=EscalationReason.DISABLED,
            budget=policy.max_recovery_attempts,
            incident=report,
            next_action=(f"Self-healing is disabled. Investigate: {report.next_action}"),
        )

    state: RecoveryState = {
        "verification": verification,
        "stop_reason": stop_reason,
        "stop_task_id": stop_task_id,
        "container": container,
        "image": image,
        "repository_path": repository_path,
        "repository": repository,
        "policy": policy,
        "human_approved": human_approved,
        "attempt": 0,
        "attempts": [],
        # Callables are carried through state so `verify` can reach them without
        # this module importing the deployment workflow.
        "_verify_fn": verify,
        "_redeploy_fn": redeploy,
    }

    # A second, independent bound. The budget is enforced in the graph; this is
    # here so that a future routing bug cannot turn the loop unbounded. Sized
    # generously for one full pass per attempt (7 nodes) plus entry and exit, and
    # never derived from anything a caller controls beyond the capped budget.
    step_limit = policy.max_recovery_attempts * 10 + 20
    try:
        result = await recovery_graph.ainvoke(state, config={"recursion_limit": step_limit})
    except GraphRecursionError:
        logger.error(
            "recovery graph hit its step limit; escalating",
            extra={"step_limit": step_limit, "budget": policy.max_recovery_attempts},
        )
        return RecoveryOutcome(
            recovered=False,
            attempts=[],
            attempts_used=policy.max_recovery_attempts,
            budget=policy.max_recovery_attempts,
            escalated=True,
            escalation_reason=EscalationReason.BUDGET_EXHAUSTED,
            next_action=(
                "The recovery loop exceeded its step limit and was stopped. This is a "
                "bug in the loop, not a deployment problem; escalate to a human."
            ),
        )
    attempts = list(result.get("attempts", []))
    incident = result.get("incident")
    escalation = result.get("escalation")
    recovered = any(attempt.succeeded for attempt in attempts)

    return RecoveryOutcome(
        recovered=recovered,
        attempts=attempts,
        attempts_used=int(result.get("attempt", 0)),
        budget=policy.max_recovery_attempts,
        escalated=bool(escalation),
        escalation_reason=escalation,
        incident=incident,
        next_action=result.get("next_action") or (incident.next_action if incident else ""),
        final_verification=result.get("last_verification"),
    )


__all__ = [
    "RecoveryState",
    "build_recovery_graph",
    "debug_node",
    "recovery_graph",
    "run_recovery",
]
