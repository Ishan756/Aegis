"""Tool execution policy.

Every MCP invocation passes through :meth:`ToolExecutionPolicy.evaluate` before a
single byte is sent to a server. The policy is deny-by-default:

1. the tool must have been discovered on a configured server, so an invented
   tool name cannot reach a server;
2. tool names matching command-execution patterns are refused outright, which is
   what stops Aegis being turned into a general-purpose shell;
3. anything not marked read-only needs explicit human approval, which must be
   recorded on the request before the call proceeds.

Refusing is always the answer when a signal is ambiguous. A policy that guesses
wrong in the permissive direction is a policy breach, not a usability issue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.models.mcp import (
    PolicyDecision,
    ToolCallRequest,
    ToolDefinition,
    ToolRiskLevel,
)

logger = get_logger(__name__)

#: Matches tool names that execute commands or arbitrary code. These are refused
#: no matter what a server advertises: a compromised or hostile server must not be
#: able to offer Aegis a shell. Matching is on word boundaries inside the name, so
#: "get_system_info" is unaffected while "run_command" is not.
#:
#: Note that "system" is deliberately absent, since harmless tools such as
#: get_system_info would be caught by it; the compound forms are listed instead.
COMMAND_EXECUTION_PATTERN = re.compile(
    r"(?:^|[_\-.])"
    r"(exec|execute|command|cmd|run|shell|bash|sh|eval|spawn|popen|process|"
    r"terminal|subprocess|powershell|interpreter|evaljs|os_system|system_call)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)

#: Same idea for argument keys: no argument may carry a shell command.
FORBIDDEN_ARGUMENT_KEY_PATTERN = re.compile(
    r"(?:^|[_\-.])(command|cmd|script|shell|exec|code)(?:$|[_\-.])", re.IGNORECASE
)

#: Largest serialised argument payload accepted, guarding against a tool call
#: that streams an entire file into memory.
MAX_ARGUMENT_BYTES = 64 * 1024

#: Risk assigned when a server gives no usable annotation.
DEFAULT_RISK: ToolRiskLevel = "medium"


def classify_risk(
    *,
    read_only: bool | None,
    destructive: bool | None,
) -> tuple[ToolRiskLevel, bool, bool]:
    """Derive risk from a server's tool annotations.

    MCP servers advertise ``readOnlyHint``, ``destructiveHint`` and
    ``idempotentHint``. Unset hints are treated as unsafe, because the default in
    the protocol is that a tool may do anything.
    """
    if destructive:
        return "high", False, True
    if read_only:
        return "low", True, False
    # Not marked read-only and not marked destructive: assume it can change
    # something, which is medium risk rather than low.
    return DEFAULT_RISK, False, False


def is_command_execution_tool(name: str) -> bool:
    """Whether a tool name looks like unrestricted command execution."""
    return COMMAND_EXECUTION_PATTERN.search(name) is not None


@dataclass(slots=True)
class ToolExecutionPolicy:
    """Decides whether a tool call may proceed.

    ``allowlist`` is optional: when empty, every discovered tool that passes the
    command-execution check is permitted. Populating it narrows access to a fixed
    set, which is the stronger configuration and what production should use.
    """

    # Low risk means read-only, and a read-only call is safe to run without a
    # human in the loop; everything from "medium" upward needs sign-off.
    require_approval_above: ToolRiskLevel = "medium"
    destructive_tools_require_approval: bool = True
    allowlist: frozenset[str] = frozenset()
    _tools: dict[str, ToolDefinition] = field(default_factory=dict)

    # -- Registry ---------------------------------------------------------

    def register(self, tools: list[ToolDefinition]) -> None:
        """Record the tools discovered across all servers."""
        self._tools = {tool.qualified_name: tool for tool in tools}

    @property
    def tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def resolve(self, name: str, server: str | None = None) -> ToolDefinition | None:
        """Find a discovered tool by qualified or bare name."""
        if name in self._tools:
            return self._tools[name]
        candidates = [
            tool
            for tool in self._tools.values()
            if tool.name == name and (server is None or tool.server == server)
        ]
        # Bare names must be unambiguous; a tool name can exist on two servers.
        return candidates[0] if len(candidates) == 1 else None

    def effective_risk(self, name: str, server: str | None = None) -> ToolRiskLevel:
        """Risk the policy assigns, which may exceed what the server claimed."""
        tool = self.resolve(name, server)
        if tool is None:
            return "high"
        if is_command_execution_tool(tool.name):
            return "critical"
        return tool.risk_level

    # -- Evaluation -------------------------------------------------------

    def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        """Assess a request. Never raises; refusal is a decision."""
        risk = self.effective_risk(request.tool_name, request.server)
        tool = self.resolve(request.tool_name, request.server)

        if tool is None:
            reason = f"Tool {request.tool_name!r} was not discovered on any configured server."
            logger.warning(
                "tool call refused",
                extra={
                    "tool": request.tool_name,
                    "reason": reason,
                    "requested_by": request.requested_by,
                },
            )
            return PolicyDecision(
                allowed=False,
                approved=False,
                requires_approval=False,
                risk_level=risk,
                reason=reason,
            )

        if is_command_execution_tool(tool.name):
            reason = (
                f"Tool {tool.qualified_name!r} executes commands; Aegis does not allow "
                "unrestricted command execution."
            )
            logger.warning(
                "tool call refused",
                extra={
                    "tool": tool.qualified_name,
                    "reason": reason,
                    "requested_by": request.requested_by,
                },
            )
            return PolicyDecision(
                allowed=False,
                approved=False,
                requires_approval=False,
                risk_level="critical",
                reason=reason,
            )

        if (
            self.allowlist
            and tool.qualified_name not in self.allowlist
            and tool.name not in self.allowlist
        ):
            reason = f"Tool {tool.qualified_name!r} is not in the configured allowlist."
            logger.warning(
                "tool call refused",
                extra={
                    "tool": tool.qualified_name,
                    "reason": reason,
                    "requested_by": request.requested_by,
                },
            )
            return PolicyDecision(
                allowed=False,
                approved=False,
                requires_approval=False,
                risk_level=risk,
                reason=reason,
            )

        argument_problem = self._check_arguments(request.arguments)
        if argument_problem:
            logger.warning(
                "tool call refused",
                extra={
                    "tool": tool.qualified_name,
                    "reason": argument_problem,
                    "requested_by": request.requested_by,
                },
            )
            return PolicyDecision(
                allowed=False,
                approved=False,
                requires_approval=False,
                risk_level=risk,
                reason=argument_problem,
            )

        requires_approval = self._requires_approval(tool, risk)
        approved = (not requires_approval) or request.approval_granted

        reason = None
        if not approved:
            reason = (
                f"Tool {tool.qualified_name!r} is {risk} risk and requires explicit "
                "human approval before it may run."
            )

        logger.info(
            "tool call evaluated",
            extra={
                "tool": tool.qualified_name,
                "risk_level": risk,
                "requires_approval": requires_approval,
                "approved": approved,
                "requested_by": request.requested_by,
            },
        )
        return PolicyDecision(
            allowed=True,
            approved=approved,
            requires_approval=requires_approval,
            risk_level=risk,
            reason=reason,
        )

    # -- Internals --------------------------------------------------------

    def _requires_approval(self, tool: ToolDefinition, risk: ToolRiskLevel) -> bool:
        if self.destructive_tools_require_approval and tool.destructive:
            return True
        return _RISK_ORDER[risk] >= _RISK_ORDER[self.require_approval_above]

    @staticmethod
    def _check_arguments(arguments: dict[str, Any]) -> str | None:
        """Validate argument shape without interpreting its values."""
        problem = _find_forbidden_key(arguments)
        if problem:
            return f"Argument {problem!r} looks like a shell command and was refused."
        try:
            encoded = json.dumps(arguments, default=None)
        except (TypeError, ValueError) as exc:
            return f"Arguments must be JSON-serialisable: {exc}"
        if encoded is None:
            return "Arguments must be JSON-serialisable."
        if len(encoded) > MAX_ARGUMENT_BYTES:
            return f"Arguments exceed the {MAX_ARGUMENT_BYTES} byte limit."
        return None


_RISK_ORDER: dict[ToolRiskLevel, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

#: Depth limit for the argument walk. Nested arguments are legitimate, but an
#: unbounded walk would itself be a denial-of-service vector.
MAX_ARGUMENT_DEPTH = 8


def _find_forbidden_key(value: Any, depth: int = 0) -> str | None:
    """Return the first key that looks like a shell command, at any depth.

    Only inspecting top-level keys would be trivially bypassed by nesting the
    command one level down, so the whole structure is walked. Depth is capped so a
    deeply nested payload cannot exhaust the stack.
    """
    if depth > MAX_ARGUMENT_DEPTH:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and FORBIDDEN_ARGUMENT_KEY_PATTERN.search(key):
                return key
            found = _find_forbidden_key(item, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_forbidden_key(item, depth + 1)
            if found:
                return found
    return None


def build_policy(
    *,
    destructive_tools_require_approval: bool = True,
    require_approval_above: ToolRiskLevel = "medium",
    allowlist: frozenset[str] = frozenset(),
) -> ToolExecutionPolicy:
    """Construct a policy from configuration."""
    return ToolExecutionPolicy(
        require_approval_above=require_approval_above,
        destructive_tools_require_approval=destructive_tools_require_approval,
        allowlist=allowlist,
    )
