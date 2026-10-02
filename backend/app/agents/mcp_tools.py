"""MCP tool graph.

    START → discover_tools → build_call_request → evaluate_policy → invoke_tool → END

The graph is deliberately split rather than being one ``call_tool`` step. Discovery,
request construction, policy evaluation and invocation are separate nodes because
they have different failure modes and different security relevance: only
``invoke_tool`` can cause a side effect, and everything before it is pure metadata
work that can be inspected and tested without launching a server.

Nothing bypasses the policy node. It is not optional and has no override flag,
which is what makes "a tool call was refused" a property of the architecture rather
than of a caller's good behaviour.

``discover_tools`` is cheap and re-reads the registry, so the graph always plans
against the tools that exist right now rather than a stale snapshot.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.models.mcp import (
    MCPToolState,
    PolicyDecision,
    ToolCallRequest,
    ToolCallResult,
    ToolDefinition,
)
from app.services.mcp_manager import get_manager

logger = get_logger(__name__)


async def discover_tools(state: MCPToolState) -> dict[str, object]:
    """Record what the configured servers currently expose."""
    manager = get_manager()
    tools: list[ToolDefinition] = await manager.discover_tools()
    logger.info(
        "mcp tools discovered",
        extra={"requested_by": state["requested_by"], "tool_count": len(tools)},
    )
    return {"tools": tools}


def build_call_request(state: MCPToolState) -> dict[str, object]:
    """Turn the caller's intent into a fully described request.

    Risk is filled in from the discovered metadata rather than from the caller, so
    an agent cannot talk its way past the approval threshold by claiming its call is
    low risk. The requesting agent is recorded on every request for the audit log.
    """
    tools = state["tools"]
    name = state["tool_name"]
    known = {tool.qualified_name: tool for tool in tools}

    if name not in known:
        available = ", ".join(sorted(known)) or "none"
        raise ValidationError(f"Unknown MCP tool {name!r}. Available tools: {available}.")

    tool = known[name]
    request = ToolCallRequest(
        tool_name=tool.qualified_name,
        arguments=state.get("arguments") or {},
        risk_level=tool.risk_level,
        requested_by=state["requested_by"],
        approval_granted=state.get("approval_granted", False),
        approval_reference=state.get("approval_reference"),
        server=tool.server,
        reason=state.get("reason"),
    )
    return {"request": request}


async def evaluate_policy(state: MCPToolState) -> dict[str, object]:
    """Decide whether the request may proceed.

    A refusal is recorded in the state rather than raised, so callers get a
    structured explanation of *why* a call was refused instead of an exception.
    """
    request = state["request"]
    decision = get_manager().policy.evaluate(request)

    if not decision.allowed:
        logger.warning(
            "mcp tool call refused by policy",
            extra={
                "tool": request.tool_name,
                "risk_level": decision.risk_level,
                "requested_by": request.requested_by,
                "reason": decision.reason,
            },
        )

    return {"decision": decision}


async def invoke_tool(state: MCPToolState) -> dict[str, object]:
    """Execute the call through the manager.

    The manager re-checks policy and returns a refusal result when the decision
    above was not to proceed, so a refused call surfaces as structured output the
    caller can report rather than as an exception. Evaluating twice is deliberate:
    this node makes the decision visible in the topology and in the logs, and the
    manager remains the layer that actually enforces it.
    """
    request: ToolCallRequest = state["request"]
    decision: PolicyDecision = state["decision"]

    if not decision.allowed or not decision.approved:
        logger.info(
            "mcp tool call not invoked; policy did not approve it",
            extra={"tool": request.tool_name, "risk_level": decision.risk_level},
        )

    result: ToolCallResult = await get_manager().call_tool(request)
    return {"result": result}


def build_graph() -> CompiledStateGraph:
    """Compile the MCP tool graph."""
    graph = StateGraph(MCPToolState)
    graph.add_node("discover_tools", discover_tools)
    graph.add_node("build_call_request", build_call_request)
    graph.add_node("evaluate_policy", evaluate_policy)
    graph.add_node("invoke_tool", invoke_tool)

    graph.add_edge(START, "discover_tools")
    graph.add_edge("discover_tools", "build_call_request")
    graph.add_edge("build_call_request", "evaluate_policy")
    graph.add_edge("evaluate_policy", "invoke_tool")
    graph.add_edge("invoke_tool", END)
    return graph.compile()


graph = build_graph()


async def discover_mcp_tools() -> list[ToolDefinition]:
    """List currently available tools without building a call request."""
    manager = get_manager()
    return await manager.discover_tools()


async def call_mcp_tool(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    requested_by: str,
    approval_granted: bool = False,
    approval_reference: str | None = None,
    reason: str | None = None,
) -> tuple[ToolCallResult | None, PolicyDecision | None, list[ToolDefinition]]:
    """Run a tool end to end.

    Returns the result, the policy decision, and the tools that were available, so
    a caller can report why a call did not happen without re-querying the servers.
    The decision is returned even when no result exists, because "refused" is
    information the caller needs.
    """
    final: dict[str, Any] = await graph.ainvoke(
        {
            "tool_name": tool_name,
            "arguments": arguments or {},
            "requested_by": requested_by,
            "approval_granted": approval_granted,
            "approval_reference": approval_reference,
            "reason": reason,
            "tools": [],
            "request": None,
            "decision": None,
            "result": None,
        },
        config={"recursion_limit": 10},
    )
    return final.get("result"), final.get("decision"), final.get("tools") or []


mcp_graph = graph
