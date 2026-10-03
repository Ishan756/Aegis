"""Deployment verification endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse

from app.agents.deployment_verification import verify_deployment
from app.models.verification import VerificationRequest, VerificationResult

router = APIRouter(prefix="/verification", tags=["verification"])


@router.post(
    "/deployment",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Verify a completed deployment",
    description=(
        "Checks that a deployment actually works, in order: the container exists, "
        "it is running, the expected port is published and answers, the health "
        "endpoint responds, it returns the expected HTTP status, the logs contain no "
        "obvious fatal errors, and declared dependencies are reachable where testable.\n\n"
        "Returns SUCCESS, WARNING or FAILED. WARNING is a real state: the deployment "
        "is serving, but something could not be confirmed or the log is unhappy. "
        "Collapsing that into either success or failure would mislead.\n\n"
        "Read-only throughout, using only tools annotated read-only. Verification "
        "never starts, stops or rebuilds anything. Every check carries the evidence "
        "it was based on. Pass `?format=markdown` for the rendered form."
    ),
)
async def verify_deployment_endpoint(
    payload: VerificationRequest,
    format: Literal["json", "markdown"] = "json",  # noqa: A002 - the documented query name
) -> VerificationResult | PlainTextResponse:
    """Verify ``payload.container_name``.

    Async because every observation is an awaited MCP round trip to a subprocess.
    """
    result = await verify_deployment(payload)

    if format == "markdown":
        return PlainTextResponse(result.summary_markdown(), media_type="text/plain; charset=utf-8")
    return result
