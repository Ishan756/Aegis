"""MCP contracts.

The boundary between the agent and any MCP server. An agent never sees a raw MCP
tool; it sees a :class:`ToolDefinition`, and never invokes anything except by
submitting a :class:`ToolCallRequest` that carries its own risk and approval
metadata. That is what makes every call auditable and policy-checkable.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

ToolRiskLevel = Literal["low", "medium", "high", "critical"]


class ToolDefinition(BaseModel):
    """Metadata for one tool discovered on one MCP server."""

    name: str = Field(description="Tool name as the server reports it.")
    qualified_name: str = Field(description="Server-qualified name, e.g. 'demo.get_system_info'.")
    server: str = Field(description="Name of the MCP server hosting the tool.")
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    risk_level: ToolRiskLevel = Field(
        description="Risk assigned from the server's tool annotations."
    )
    read_only: bool = Field(default=False, description="Server marked the tool read-only.")
    destructive: bool = Field(default=False, description="Server marked the tool destructive.")


class ToolCallRequest(BaseModel):
    """A single proposed tool invocation.

    Every field required by the execution policy travels with the call: the tool
    name, its arguments, the assessed risk, the requesting agent, and whether
    human approval is required. Nothing may be invoked without one of these.
    """

    tool_name: str = Field(description="Tool name, qualified or bare.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: ToolRiskLevel = "medium"
    requested_by: str = Field(description="Agent or component asking for the call.")
    requires_approval: bool | None = Field(
        default=None,
        description=(
            "Whether the caller believes approval is required. None means no claim was "
            "made; the execution policy decides, and a mismatch is logged."
        ),
    )
    approval_granted: bool = Field(default=False)
    approval_reference: str | None = Field(
        default=None, description="Who approved the call, for the audit trail."
    )
    server: str | None = Field(
        default=None, description="Pin to one server; otherwise the registry resolves it."
    )
    reason: str | None = Field(default=None, description="Why the agent wants this call.")

    @field_validator("arguments")
    @classmethod
    def _reject_binary_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Refuse arguments carrying NUL bytes, which truncate paths downstream."""
        for key, item in value.items():
            if isinstance(item, str) and "\x00" in item:
                raise ValueError(f"argument {key!r} contains a NUL byte")
            if isinstance(item, (list, tuple)):
                for element in item:
                    if isinstance(element, str) and "\x00" in element:
                        raise ValueError(f"argument {key!r} contains a NUL byte")
        return value


class PolicyDecision(BaseModel):
    """Outcome of evaluating a request against the execution policy."""

    allowed: bool = Field(description="Whether the call may proceed at all.")
    approved: bool = Field(description="Allowed and past the approval gate.")
    requires_approval: bool
    risk_level: ToolRiskLevel
    reason: str | None = None


class ToolCallResult(BaseModel):
    """Outcome of a tool invocation, successful or not.

    Failures are values, not exceptions: the agent needs to reason about a
    refused or timed-out call, not just catch it.
    """

    tool_name: str
    qualified_name: str
    server: str
    success: bool
    content: Any = None
    is_error: bool = False
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: float = Field(default=0.0, ge=0.0)
    requested_by: str | None = None
    requires_approval: bool = False


class ToolCatalogResponse(BaseModel):
    """The tools currently reachable through the configured MCP servers."""

    servers: list[str] = Field(default_factory=list)
    tools: list[ToolDefinition] = Field(default_factory=list)
    count: int = 0
    tool_names: list[str] = Field(default_factory=list)


class MCPToolState(TypedDict, total=False):
    """LangGraph state for the MCP tool workflow."""

    tool_name: str
    arguments: dict[str, Any]
    requested_by: str
    approval_granted: bool
    approval_reference: str | None
    reason: str | None
    tools: list[ToolDefinition]
    request: ToolCallRequest
    decision: PolicyDecision
    result: ToolCallResult
