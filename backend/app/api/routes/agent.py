"""Agent endpoints.

Mounted at ``/api`` rather than ``/api/v1`` so the planning endpoint sits at
``POST /api/agent/plan`` as specified. Agent routes are still pre-1.0 and may
change shape; revisit the prefix when this surface stabilises.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.agents.planner import planning_graph
from app.core.exceptions import ValidationError
from app.models.planning import AgentPlanRequest, PlanResponse

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post(
    "/plan",
    response_model=PlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate a DevOps execution plan",
    description=(
        "Runs the planning graph and returns a structured, not-yet-executed plan: "
        "objective, task type, assumptions, required tools, ordered steps, risk "
        "level, approval requirement, and expected verification."
    ),
)
def create_plan(payload: AgentPlanRequest) -> PlanResponse:
    """Plan the work described by ``payload.request``.

    Synchronous because the whole graph is CPU-bound string work today. It moves
    to a background run as soon as plan execution exists and takes real time.
    """
    state = planning_graph.invoke({"request": payload.request})
    validation = state["validation"]

    if not validation.is_valid:
        # Surface the validator's reasoning through the standard error envelope
        # rather than returning a 200 with a broken plan attached.
        raise ValidationError(
            "The generated plan failed validation.",
            details={"issues": validation.issues},
        )

    return PlanResponse(
        plan=state["plan"],
        analysis=state["analysis"],
        validation=validation,
    )
