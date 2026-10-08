"""MCP client manager.

Owns the lifecycle of every configured MCP server: spawning the configured
command over stdio, initialising a session, discovering tools, and invoking them
under a timeout with full audit logging.

Two properties matter more than anything else here:

**Only configured servers can be reached.** Commands come from
``AEGIS_MCP__SERVERS`` and are split with :func:`shlex.split`, never handed to a
shell, so no request can introduce a new command to run.

**Failures are values.** A refused, missing, timed-out or broken call returns a
:class:`~app.models.mcp.ToolCallResult` with an error code, because an agent
needs to reason about a failed tool call rather than only catch an exception.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError as McpError

from app.core.config import MCPSettings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.models.mcp import (
    ToolCallRequest,
    ToolCallResult,
    ToolDefinition,
)
from app.services.mcp_policy import ToolExecutionPolicy, build_policy, classify_risk

logger = get_logger(__name__)

#: Extra head-room over the tool timeout before the whole call is abandoned.
_CONNECT_TIMEOUT_SECONDS = 20.0


class MCPClientManager:
    """Connects to the configured MCP servers and exposes their tools.

    Use as an async context manager so server processes are always reaped::

        async with MCPClientManager(settings.mcp) as manager:
            tools = await manager.list_tools()
    """

    def __init__(
        self,
        settings: MCPSettings,
        policy: ToolExecutionPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy or build_policy(
            destructive_tools_require_approval=settings.destructive_tools_require_approval
        )
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[ToolDefinition] = []
        self._closed = False

    # -- Lifecycle --------------------------------------------------------

    async def __aenter__(self) -> MCPClientManager:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def policy(self) -> ToolExecutionPolicy:
        return self._policy

    @property
    def connected_servers(self) -> list[str]:
        return sorted(self._sessions)

    async def connect(self) -> None:
        """Open a session to every configured server.

        A server that fails to start is logged and skipped rather than taking the
        whole manager down: one broken MCP server should not break Aegis.
        """
        if not self._settings.enabled:
            logger.info("MCP is disabled; no servers will be contacted")
            return

        for name, command in self._settings.servers.items():
            try:
                await self._connect_one(name, command)
            except (TimeoutError, OSError, ValueError, McpError) as exc:
                # Includes FileNotFoundError for a bad command path.
                logger.error(
                    "MCP server failed to start", extra={"server": name, "error": str(exc)}
                )

        if self._sessions:
            await self.discover_tools()

    async def _connect_one(self, name: str, command: str) -> None:
        # shlex.split parses arguments without invoking a shell, so a command
        # containing "; rm -rf /" is split into inert argv entries, not run.
        argv = shlex.split(command)
        if not argv:
            raise ValueError("empty command")

        params = StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env=self._server_environment(),
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))

        await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT_SECONDS)
        self._sessions[name] = session
        logger.info("MCP server connected", extra={"server": name, "command": argv[0]})

    def _server_environment(self) -> dict[str, str] | None:
        """Build the environment handed to each server subprocess.

        Only the variables named in ``AEGIS_MCP__FORWARD_ENVIRONMENT`` are
        passed. The SDK merges this over its own safe allowlist, so PATH still
        reaches the process and the interpreter can be found.

        A credential travels through the environment rather than argv on purpose:
        argv is visible to any process on the host via ``ps``, the environment of
        another user's process is not.
        """
        forwarded = self.forwarded_environment
        if forwarded:
            logger.info(
                "forwarding environment to MCP servers",
                # Names only. Logging the values here would put the token in the log.
                extra={"server": "*", "variables": sorted(forwarded)},
            )
        return forwarded or None

    @property
    def forwarded_environment(self) -> dict[str, str]:
        """The variables named in ``AEGIS_MCP__FORWARD_ENVIRONMENT`` that are set.

        Exposed so a caller opening a :meth:`scoped_server` can build on the same
        forwarding rules rather than inventing a second set.
        """
        names = self._settings.forward_environment
        return {name: os.environ[name] for name in names if name in os.environ}

    @asynccontextmanager
    async def scoped_server(
        self,
        name: str,
        *,
        command: str,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[ClientSession]:
        """Connect one extra server for the duration of a ``async with`` block.

        The long-lived servers are configured once at startup, so they share one
        environment. A deployment target needs the *same* server code pointed
        somewhere else — the Docker CLI with ``DOCKER_HOST`` set at a remote
        daemon — and giving the shared server that variable would repoint every
        local deployment at the remote host too. So the target-specific server is
        a separate process with its own environment, torn down when the block
        ends.

        Its tools are registered with the policy under ``name`` for the duration,
        so calls through :meth:`call_tool` are evaluated exactly like any other:
        an invented tool name is still refused, and a mutating tool still needs
        approval. Unregistering on exit is what stops a name from surviving its
        server — a registered tool whose session is gone would fail later with
        "server_unavailable" instead of "not discovered".

        Not safe to use concurrently: two blocks would race on the shared
        registry. Deployments are sequential by design, and a lock would only
        disguise that assumption rather than enforce anything about ordering.
        """
        if self._closed:
            raise ConfigurationError("The MCP client manager is closed.")
        if not self._settings.enabled:
            raise ConfigurationError(
                "MCP is disabled (AEGIS_MCP__ENABLED=false); a scoped server cannot start."
            )
        if name in self._sessions:
            raise ConfigurationError(f"MCP server {name!r} is already connected.")

        argv = shlex.split(command)
        if not argv:
            raise ConfigurationError(f"The command for MCP server {name!r} is empty.")

        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=argv[0],
                args=argv[1:],
                env=env or {},
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT_SECONDS)
            response = await asyncio.wait_for(
                session.list_tools(),
                timeout=self._settings.request_timeout_seconds,
            )
            definitions = [self._to_definition(name, tool) for tool in response.tools]

            self._sessions[name] = session
            self._tools = [*self._tools, *definitions]
            self._policy.register(self._tools)
            logger.info(
                "scoped MCP server connected",
                extra={"server": name, "command": argv[0], "tool_count": len(definitions)},
            )
            yield session
        finally:
            self._sessions.pop(name, None)
            self._tools = [tool for tool in self._tools if tool.server != name]
            self._policy.register(self._tools)
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001 - teardown must not mask the block's error
                logger.warning(
                    "scoped MCP server shutdown failed",
                    extra={"server": name, "error": str(exc)},
                )
            logger.info("scoped MCP server disconnected", extra={"server": name})

    async def close(self) -> None:
        """Shut every server process down."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._stack.aclose()
        finally:
            self._sessions.clear()
            logger.info("MCP sessions closed")

    # -- Discovery --------------------------------------------------------

    async def discover_tools(self) -> list[ToolDefinition]:
        """List every tool across every connected server."""
        discovered: list[ToolDefinition] = []

        for server, session in self._sessions.items():
            try:
                response = await asyncio.wait_for(
                    session.list_tools(),
                    timeout=self._settings.request_timeout_seconds,
                )
            except (TimeoutError, McpError, OSError) as exc:
                logger.error(
                    "MCP tool discovery failed",
                    extra={"server": server, "error": str(exc)},
                )
                continue

            for tool in response.tools:
                discovered.append(self._to_definition(server, tool))

        self._tools = discovered
        self._policy.register(discovered)
        logger.info(
            "MCP tools discovered",
            extra={
                "servers": sorted(self._sessions),
                "tool_count": len(discovered),
                "tools": [tool.qualified_name for tool in discovered],
            },
        )
        return discovered

    @staticmethod
    def _annotation(annotations: Any, snake: str, camel: str) -> bool | None:
        """Read a tool annotation hint.

        The SDK exposes these as snake_case fields, but the wire format is
        camelCase, so both spellings are accepted rather than silently defaulting
        to "unknown" and inflating every tool's risk.
        """
        if annotations is None:
            return None
        for name in (snake, camel):
            value = getattr(annotations, name, None)
            if isinstance(value, bool):
                return value
        return None

    @classmethod
    def _to_definition(cls, server: str, tool: Any) -> ToolDefinition:
        annotations = getattr(tool, "annotations", None)
        risk, read_only, destructive = classify_risk(
            read_only=cls._annotation(annotations, "read_only_hint", "readOnlyHint"),
            destructive=cls._annotation(annotations, "destructive_hint", "destructiveHint"),
        )
        return ToolDefinition(
            name=tool.name,
            qualified_name=f"{server}.{tool.name}",
            server=server,
            title=getattr(tool, "title", None),
            description=getattr(tool, "description", None),
            input_schema=getattr(tool, "input_schema", None) or {},
            output_schema=getattr(tool, "output_schema", None),
            risk_level=risk,
            read_only=read_only,
            destructive=destructive,
        )

    @property
    def tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    # -- Invocation -------------------------------------------------------

    async def call_tool(self, request: ToolCallRequest) -> ToolCallResult:
        """Evaluate the request against policy, then invoke it if permitted."""
        decision = self._policy.evaluate(request)
        tool = self._policy.resolve(request.tool_name, request.server)
        qualified = tool.qualified_name if tool else request.tool_name

        def failure(code: str, message: str, risk: str | None = None) -> ToolCallResult:
            return ToolCallResult(
                tool_name=request.tool_name,
                qualified_name=qualified,
                server=tool.server if tool else (request.server or ""),
                success=False,
                is_error=True,
                error_code=code,
                error_message=message,
                requested_by=request.requested_by,
                requires_approval=decision.requires_approval,
            )

        if not decision.allowed:
            return failure("policy_denied", decision.reason or "Refused by execution policy.")
        if not decision.approved:
            return failure("approval_required", decision.reason or "Human approval required.")

        # The request carries the caller's expectation of whether approval was
        # needed. Policy is the single source of truth, so a disagreement is worth
        # surfacing: it usually means the caller guessed at risk. The call still
        # proceeds on policy's verdict, never the caller's.
        if (
            request.requires_approval is not None
            and request.requires_approval != decision.requires_approval
        ):
            logger.warning(
                "tool call approval expectation mismatch",
                extra={
                    "tool": qualified,
                    "claimed_requires_approval": request.requires_approval,
                    "policy_requires_approval": decision.requires_approval,
                    "requested_by": request.requested_by,
                },
            )

        assert tool is not None  # decision.allowed implies resolution succeeded
        session = self._sessions.get(tool.server)
        if session is None:
            return failure("server_unavailable", f"MCP server {tool.server!r} is not connected.")

        timeout = self._settings.request_timeout_seconds
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                session.call_tool(
                    tool.name,
                    request.arguments,
                    read_timeout_seconds=timeout,
                ),
                timeout=timeout + 5.0,
            )
        except TimeoutError:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            logger.warning(
                "tool call timed out",
                extra={
                    "tool": qualified,
                    "timeout_seconds": timeout,
                    "duration_ms": elapsed,
                    "requested_by": request.requested_by,
                },
            )
            return ToolCallResult(
                tool_name=request.tool_name,
                qualified_name=qualified,
                server=tool.server,
                success=False,
                is_error=True,
                error_code="timeout",
                error_message=f"Tool {qualified!r} exceeded {timeout}s.",
                duration_ms=elapsed,
                requested_by=request.requested_by,
                requires_approval=decision.requires_approval,
            )
        except McpError as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            # The SDK surfaces a read timeout as a generic MCPError rather than
            # asyncio.TimeoutError, so it is matched here or a slow tool would be
            # reported as a malformed call.
            if _looks_like_timeout(exc):
                logger.warning(
                    "tool call timed out",
                    extra={
                        "tool": qualified,
                        "timeout_seconds": timeout,
                        "duration_ms": elapsed,
                        "requested_by": request.requested_by,
                    },
                )
                return ToolCallResult(
                    tool_name=request.tool_name,
                    qualified_name=qualified,
                    server=tool.server,
                    success=False,
                    is_error=True,
                    error_code="timeout",
                    error_message=f"Tool {qualified!r} exceeded {timeout}s.",
                    duration_ms=elapsed,
                    requested_by=request.requested_by,
                    requires_approval=decision.requires_approval,
                )

            logger.warning(
                "tool call rejected by server",
                extra={
                    "tool": qualified,
                    "error": str(exc),
                    "duration_ms": elapsed,
                    "requested_by": request.requested_by,
                },
            )
            return ToolCallResult(
                tool_name=request.tool_name,
                qualified_name=qualified,
                server=tool.server,
                success=False,
                is_error=True,
                error_code="invalid_tool",
                error_message=str(exc),
                duration_ms=elapsed,
                requested_by=request.requested_by,
                requires_approval=decision.requires_approval,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            logger.error(
                "tool call failed",
                extra={
                    "tool": qualified,
                    "error": str(exc),
                    "duration_ms": elapsed,
                    "requested_by": request.requested_by,
                },
            )
            return ToolCallResult(
                tool_name=request.tool_name,
                qualified_name=qualified,
                server=tool.server,
                success=False,
                is_error=True,
                error_code="upstream_error",
                error_message=str(exc),
                duration_ms=elapsed,
                requested_by=request.requested_by,
                requires_approval=decision.requires_approval,
            )

        elapsed = round((time.perf_counter() - started) * 1000, 3)
        content = _extract_content(response)
        # MCP signals a tool-level failure with isError while still returning a
        # response, so this is not the same as an exception above. The SDK exposes it
        # as snake_case, with the camelCase spelling accepted for older versions.
        failed = bool(getattr(response, "is_error", None) or getattr(response, "isError", False))
        succeeded = not failed

        log = logger.info if succeeded else logger.warning
        log(
            "tool call completed",
            extra={
                "tool": qualified,
                "server": tool.server,
                "success": succeeded,
                "duration_ms": elapsed,
                "risk_level": decision.risk_level,
                "requires_approval": decision.requires_approval,
                "requested_by": request.requested_by,
                "approval_reference": request.approval_reference,
            },
        )
        return ToolCallResult(
            tool_name=request.tool_name,
            qualified_name=qualified,
            server=tool.server,
            success=succeeded,
            content=content,
            is_error=not succeeded,
            error_code=None if succeeded else "tool_error",
            error_message=None if succeeded else _first_text(content),
            duration_ms=elapsed,
            requested_by=request.requested_by,
            requires_approval=decision.requires_approval,
        )


def _looks_like_timeout(exc: Exception) -> bool:
    """Whether an MCP error represents a timeout rather than a bad call.

    The SDK has no dedicated timeout exception for a read that exceeds
    ``read_timeout_seconds``; it raises ``MCPError`` with a message. Matching on the
    message is the only signal available, so it is kept narrow and explicit.
    """
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def _extract_content(response: Any) -> Any:
    """Reduce an MCP tool response to plain JSON-serialisable data."""
    structured = getattr(response, "structuredContent", None) or getattr(
        response, "structured_content", None
    )
    if structured:
        return structured
    blocks = getattr(response, "content", None) or []
    texts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
        else:
            texts.append(str(block))
    if not texts:
        return None
    if len(texts) == 1:
        return texts[0]
    return texts


def _first_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        first = content[0]
        return first if isinstance(first, str) else str(first)
    return str(content) if content is not None else None


# -- Process-wide manager ------------------------------------------------
#
# Server subprocesses are process-wide resources, so there is exactly one manager
# per application. The FastAPI lifespan owns its lifetime; ``get_manager`` exists so
# graph nodes and routes never construct a second set of connections.

_manager: MCPClientManager | None = None


def set_manager(manager: MCPClientManager | None) -> None:
    """Install the manager for this process. Called by the app lifespan."""
    global _manager
    _manager = manager


def get_manager() -> MCPClientManager:
    """Return the active manager.

    Raises rather than lazily creating one: a manager started outside the lifespan
    would leak subprocesses, so a missing manager is a startup bug worth surfacing.
    """
    if _manager is None:
        raise ConfigurationError(
            "The MCP client manager is not running. MCP must be enabled and started "
            "during application startup."
        )
    return _manager
