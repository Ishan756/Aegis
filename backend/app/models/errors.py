"""Error response schemas.

Every error Aegis returns — validation failures, HTTP errors and unexpected
exceptions alike — uses this single envelope, so a client needs exactly one
parsing path.

.. code-block:: json

    {
      "error": {
        "code": "not_found",
        "message": "Deployment dep_123 was not found.",
        "details": {"resource": "deployment", "id": "dep_123"},
        "correlation_id": "3f2a1c8e-9b7d-4f21-8c6a-1d5e7f9a0b3c"
      }
    }
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """The single error shape returned by the API."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable summary safe to show users.")
    details: dict[str, Any] | list[Any] | None = Field(
        default=None, description="Optional structured context."
    )
    correlation_id: str | None = Field(
        default=None, description="ID tying this error to the server logs."
    )


class ErrorResponse(BaseModel):
    """Top-level error envelope."""

    error: ErrorBody
