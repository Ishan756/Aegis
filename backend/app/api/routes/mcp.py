"""MCP tool endpoints.

Two read paths over the same machinery the agent uses: what tools exist, and what
happens when one is called. They are exposed for observability and manual testing,
not as a way around the agent: both go through the same policy node, so a refusal
here is the same refusal the agent would get.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.agents.mcp_tools import call_mcp_tool, discover_mcp_tools
from app.core.exceptions import AegisError
from app.models.mcp import ToolCallRequest, ToolCallResult, ToolCatalogResponse
from app.services.mcp_manager import get_manager

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get(
    "/tools",
    response_model=ToolCatalogResponse,
    status_code=status.HTTP_200_OK,
    summary="List tools exposed by the configured MCP servers",
    description=(
        "Contacts the configured MCP servers and returns their tools with input and "
        "output schemas. Each tool is annotated with the risk Aegis assigned from the "
        "server's own hints, and whether it requires human approval."
    ),
)
async def list_tools() -> ToolCatalogResponse:
    """Return the current tool catalog."""
    manager = get_manager()
    tools = await discover_mcp_tools()
    return ToolCatalogResponse(
        servers=manager.connected_servers,
        tools=tools,
        count=len(tools),
        tool_names=[tool.qualified_name for tool in tools],
    )


@router.post(
    "/tools/call",
    response_model=ToolCallResult,
    status_code=status.HTTP_200_OK,
    summary="Call an MCP tool",
    description=(
        "Runs one tool on a configured server. The request passes through the same "
        "execution policy the agent uses: unknown tools and command-execution tools are "
        "refused, and anything above the approval threshold is refused until "
        "'approval_granted' is set. Refusals are returned in the body with HTTP 200 and "
        "'success' false, because a refusal is a recorded outcome rather than a fault in "
        "the request."
    ),
)
async def call_tool(payload: ToolCallRequest) -> ToolCallResult:
    """Invoke a tool, returning the result or the reason it did not run."""
    result, _decision, _tools = await call_mcp_tool(
        tool_name=payload.tool_name,
        arguments=payload.arguments,
        requested_by=payload.requested_by,
        approval_granted=payload.approval_granted,
        approval_reference=payload.approval_reference,
        reason=payload.reason,
    )
    if result is None:
        raise AegisError("The MCP graph produced no result for a completed run.")
    return result
