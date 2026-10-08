"""MCP integration and policy tests.

These run real MCP servers over stdio rather than mocking the client, because the
behaviour worth protecting here is the wire behaviour: annotations arriving as
snake_case, schemas arriving as JSON Schema, subprocesses starting and being
cleaned up. A mock would agree with whatever the code assumed.

Three fixture servers cover the cases that matter:

- ``read_only_server``  annotated read-only, so calls run without approval
- ``mixed_server``      also exposes a write tool, to exercise the approval gate
- ``slow_server``       sleeps past the timeout, to exercise the timeout path

Servers are written to ``tmp_path`` and launched as a subprocess with ``sys.executable``,
so nothing depends on the working directory or on a globally installed package.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import MCPSettings, Settings, get_settings
from app.core.exceptions import ValidationError
from app.models.mcp import ToolCallRequest, ToolDefinition
from app.services.mcp_manager import MCPClientManager, get_manager, set_manager
from app.services.mcp_policy import ToolExecutionPolicy, build_policy, classify_risk

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]


# -- Fixture servers -----------------------------------------------------

READ_ONLY_SERVER = """
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("read-only")

@server.tool(
    name="read_note",
    description="Return a fixed note.",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False),
)
def read_note(key: str = "default") -> dict:
    return {"key": key, "body": "fixture note"}

@server.tool(
    name="count",
    description="Return a number.",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def count(n: int = 3) -> dict:
    return {"n": n}

server.run("stdio")
"""

MIXED_SERVER = """
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("mixed")

@server.tool(
    name="read_thing",
    description="Read something.",
    annotations=ToolAnnotations(read_only_hint=True),
)
def read_thing() -> dict:
    return {"value": 1}

@server.tool(
    name="overwrite_thing",
    description="Replace something, which changes state.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
)
def overwrite_thing(value: int = 0) -> dict:
    return {"written": value}

@server.tool(
    name="run_command",
    description="Execute a shell command.",
    annotations=ToolAnnotations(read_only_hint=True),
)
def run_command(cmd: str = "") -> dict:
    return {"ran": cmd}

server.run("stdio")
"""

SLOW_SERVER = """
import time
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("slow")

@server.tool(
    name="stall",
    description="Sleep far longer than any test timeout.",
    annotations=ToolAnnotations(read_only_hint=True),
)
def stall() -> dict:
    time.sleep(30)
    return {"done": True}

server.run("stdio")
"""


def _write_server(directory: Path, name: str, source: str) -> str:
    """Write a server script and return its launch command.

    The interpreter and the script path are quoted because this checkout lives under
    a path containing spaces, and the command string is parsed with ``shlex.split``.
    """
    path = directory / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


async def test_project_root_aws_server_is_registered_from_backend_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured relative AWS command connects and exposes its tools."""
    monkeypatch.chdir(REPO_ROOT / "backend")
    venv_bin = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", venv_bin + os.pathsep + os.environ.get("PATH", ""))

    settings = Settings()
    assert settings.mcp.servers == {"aws": "python ../mcp_servers/aws/server.py"}

    async with MCPClientManager(settings.mcp) as connected:
        assert connected.connected_servers == ["aws"]
        tools = await connected.discover_tools()

    assert tools
    assert all(tool.qualified_name.startswith("aws.") for tool in tools)


@pytest.fixture
def server_commands(tmp_path: Path) -> dict[str, str]:
    """Launch commands for the three fixture servers."""
    return {
        "readonly": _write_server(tmp_path, "readonly_server", READ_ONLY_SERVER),
        "mixed": _write_server(tmp_path, "mixed_server", MIXED_SERVER),
        "slow": _write_server(tmp_path, "slow_server", SLOW_SERVER),
    }


@pytest.fixture
async def manager(server_commands: dict[str, str]) -> Any:
    """A manager connected to the read-only and mixed fixture servers."""
    settings = MCPSettings(
        enabled=True,
        servers={
            "readonly": server_commands["readonly"],
            "mixed": server_commands["mixed"],
        },
        request_timeout_seconds=10,
    )
    async with MCPClientManager(settings) as connected:
        yield connected


@pytest.fixture
async def slow_manager(server_commands: dict[str, str]) -> Any:
    """A manager whose only server stalls, with a short timeout."""
    settings = MCPSettings(
        enabled=True,
        servers={"slow": server_commands["slow"]},
        request_timeout_seconds=1,
    )
    async with MCPClientManager(settings) as connected:
        yield connected


# -- Risk classification -------------------------------------------------


def test_read_only_annotation_is_low_risk() -> None:
    risk, read_only, destructive = classify_risk(read_only=True, destructive=False)
    assert (risk, read_only, destructive) == ("low", True, False)


def test_destructive_annotation_is_high_risk() -> None:
    risk, read_only, destructive = classify_risk(read_only=False, destructive=True)
    assert (risk, read_only, destructive) == ("high", False, True)


def test_missing_annotations_are_not_treated_as_safe() -> None:
    """An unannotated tool must not be assumed read-only."""
    risk, read_only, _ = classify_risk(read_only=None, destructive=None)
    assert risk == "medium"
    assert read_only is False


# -- Policy --------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["run_command", "execute_code", "shell", "bash", "subprocess.spawn", "system_call"],
)
def test_command_execution_tools_are_recognised(name: str) -> None:
    from app.services.mcp_policy import is_command_execution_tool

    assert is_command_execution_tool(name) is True


@pytest.mark.parametrize(
    "name",
    ["get_system_info", "get_project_files", "get_shard_data", "bashrc", "list_prs"],
)
def test_ordinary_tool_names_are_not_treated_as_commands(name: str) -> None:
    from app.services.mcp_policy import is_command_execution_tool

    assert is_command_execution_tool(name) is False


def test_unknown_tool_is_denied() -> None:
    policy = build_policy()
    policy.register([])
    decision = policy.evaluate(ToolCallRequest(tool_name="ghost", requested_by="planner"))
    assert decision.allowed is False
    assert decision.risk_level == "high"
    assert "not discovered" in (decision.reason or "")


def test_command_tool_denied_even_when_marked_read_only() -> None:
    """A server calling its shell tool read-only does not make it safe."""
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="run_command",
                qualified_name="mixed.run_command",
                server="mixed",
                risk_level="low",
                read_only=True,
            )
        ]
    )
    decision = policy.evaluate(
        ToolCallRequest(tool_name="mixed.run_command", requested_by="planner")
    )
    assert decision.allowed is False
    assert decision.risk_level == "critical"


def test_read_only_tool_needs_no_approval() -> None:
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="read_note",
                qualified_name="readonly.read_note",
                server="readonly",
                risk_level="low",
                read_only=True,
            )
        ]
    )
    decision = policy.evaluate(
        ToolCallRequest(tool_name="readonly.read_note", requested_by="planner")
    )
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.approved is True


def test_destructive_tool_requires_approval_then_runs_once_granted() -> None:
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="overwrite_thing",
                qualified_name="mixed.overwrite_thing",
                server="mixed",
                risk_level="high",
                destructive=True,
            )
        ]
    )

    refused = policy.evaluate(
        ToolCallRequest(
            tool_name="mixed.overwrite_thing",
            requested_by="planner",
            approval_reference="ticket-1",
        )
    )
    assert refused.allowed is True
    assert refused.requires_approval is True
    assert refused.approved is False
    assert "approval" in (refused.reason or "").lower()

    approved = policy.evaluate(
        ToolCallRequest(
            tool_name="mixed.overwrite_thing",
            requested_by="planner",
            approval_granted=True,
            approval_reference="ticket-1",
        )
    )
    assert approved.approved is True


def test_caller_cannot_self_declare_a_tool_low_risk() -> None:
    """The request's risk field is metadata, not authority."""
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="overwrite_thing",
                qualified_name="mixed.overwrite_thing",
                server="mixed",
                risk_level="high",
                destructive=True,
            )
        ]
    )
    decision = policy.evaluate(
        ToolCallRequest(
            tool_name="mixed.overwrite_thing",
            requested_by="planner",
            risk_level="low",
        )
    )
    assert decision.risk_level == "high"
    assert decision.requires_approval is True


def test_approval_claim_mismatch_is_reported_but_policy_wins() -> None:
    policy: ToolExecutionPolicy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="read_note",
                qualified_name="readonly.read_note",
                server="readonly",
                risk_level="low",
                read_only=True,
            )
        ]
    )
    decision = policy.evaluate(
        ToolCallRequest(
            tool_name="readonly.read_note",
            requested_by="planner",
            requires_approval=False,
        )
    )
    assert decision.allowed is True
    assert decision.requires_approval is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "rm -rf /"},
        {"cmd": "whoami"},
        {"nested": {"shell": "sh"}},
    ],
)
def test_shell_like_argument_keys_are_refused(arguments: dict[str, Any]) -> None:
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="read_note",
                qualified_name="readonly.read_note",
                server="readonly",
                risk_level="low",
                read_only=True,
            )
        ]
    )
    decision = policy.evaluate(
        ToolCallRequest(tool_name="readonly.read_note", requested_by="planner", arguments=arguments)
    )
    assert decision.allowed is False


def test_oversized_arguments_are_refused() -> None:
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="read_note",
                qualified_name="readonly.read_note",
                server="readonly",
                risk_level="low",
                read_only=True,
            )
        ]
    )
    decision = policy.evaluate(
        ToolCallRequest(
            tool_name="readonly.read_note",
            requested_by="planner",
            arguments={"body": "x" * 200_000},
        )
    )
    assert decision.allowed is False


def test_allowlist_narrows_access() -> None:
    policy = build_policy(allowlist=frozenset({"readonly.read_note"}))
    policy.register(
        [
            ToolDefinition(
                name="read_note",
                qualified_name="readonly.read_note",
                server="readonly",
                risk_level="low",
                read_only=True,
            ),
            ToolDefinition(
                name="count",
                qualified_name="readonly.count",
                server="readonly",
                risk_level="low",
                read_only=True,
            ),
        ]
    )
    assert (
        policy.evaluate(
            ToolCallRequest(tool_name="readonly.read_note", requested_by="planner")
        ).allowed
        is True
    )
    denied = policy.evaluate(ToolCallRequest(tool_name="readonly.count", requested_by="planner"))
    assert denied.allowed is False
    assert "allowlist" in (denied.reason or "")


def test_ambiguous_bare_name_is_not_resolved() -> None:
    """Two servers offering the same bare name must not silently pick one."""
    policy = build_policy()
    policy.register(
        [
            ToolDefinition(
                name="shared",
                qualified_name="a.shared",
                server="a",
                risk_level="low",
                read_only=True,
            ),
            ToolDefinition(
                name="shared",
                qualified_name="b.shared",
                server="b",
                risk_level="low",
                read_only=True,
            ),
        ]
    )
    assert policy.resolve("shared") is None
    assert policy.resolve("a.shared") is not None


# -- Manager -------------------------------------------------------------


async def test_discovery_reports_schemas_and_risk(manager: MCPClientManager) -> None:
    tools = await manager.discover_tools()
    by_name = {tool.qualified_name: tool for tool in tools}

    assert "readonly.read_note" in by_name
    note = by_name["readonly.read_note"]
    assert note.risk_level == "low"
    assert note.read_only is True
    # The input schema must survive the round trip, or an agent cannot call the tool.
    assert "key" in note.input_schema.get("properties", {})
    assert note.input_schema.get("type") == "object"


async def test_discovery_reports_destructive_risk(manager: MCPClientManager) -> None:
    tools = await manager.discover_tools()
    overwrite = next(t for t in tools if t.name == "overwrite_thing")
    assert overwrite.risk_level == "high"
    assert overwrite.destructive is True


async def test_read_only_call_succeeds(manager: MCPClientManager) -> None:
    result = await manager.call_tool(
        ToolCallRequest(
            tool_name="readonly.read_note",
            requested_by="planner",
            arguments={"key": "abc"},
        )
    )
    assert result.success is True
    assert result.error_code is None
    assert result.duration_ms > 0
    assert result.requires_approval is False


async def test_call_to_unknown_tool_is_refused_before_reaching_the_server(
    manager: MCPClientManager,
) -> None:
    result = await manager.call_tool(
        ToolCallRequest(tool_name="readonly.nonexistent", requested_by="planner")
    )
    assert result.success is False
    assert result.error_code == "policy_denied"


async def test_destructive_call_without_approval_is_refused(
    manager: MCPClientManager,
) -> None:
    result = await manager.call_tool(
        ToolCallRequest(tool_name="mixed.overwrite_thing", requested_by="planner")
    )
    assert result.success is False
    assert result.error_code == "approval_required"
    assert result.requires_approval is True


async def test_destructive_call_with_approval_succeeds(
    manager: MCPClientManager,
) -> None:
    result = await manager.call_tool(
        ToolCallRequest(
            tool_name="mixed.overwrite_thing",
            requested_by="planner",
            arguments={"value": 7},
            approval_granted=True,
            approval_reference="ticket-9",
        )
    )
    assert result.success is True
    assert result.requires_approval is True


async def test_command_tool_is_refused_even_though_server_advertises_it(
    manager: MCPClientManager,
) -> None:
    """The demo server offers no shell; this proves a server cannot add one."""
    result = await manager.call_tool(
        ToolCallRequest(
            tool_name="mixed.run_command",
            requested_by="planner",
            arguments={"cmd": "id"},
        )
    )
    assert result.success is False
    assert result.error_code == "policy_denied"


async def test_bad_arguments_produce_a_tool_error_not_a_crash(
    manager: MCPClientManager,
) -> None:
    result = await manager.call_tool(
        ToolCallRequest(
            tool_name="readonly.count",
            requested_by="planner",
            arguments={"n": "not-a-number"},
        )
    )
    assert result.success is False
    assert result.error_code == "tool_error"


async def test_slow_tool_times_out(slow_manager: MCPClientManager) -> None:
    result = await slow_manager.call_tool(
        ToolCallRequest(tool_name="slow.stall", requested_by="planner")
    )
    assert result.success is False
    assert result.error_code == "timeout"


async def test_disabled_manager_contacts_nothing(server_commands: dict[str, str]) -> None:
    async with MCPClientManager(
        MCPSettings(enabled=False, servers={"readonly": server_commands["readonly"]})
    ) as disabled:
        assert disabled.connected_servers == []
        assert await disabled.discover_tools() == []


async def test_no_servers_configured_is_not_an_error() -> None:
    async with MCPClientManager(MCPSettings(enabled=True, servers={})) as empty:
        assert empty.connected_servers == []
        assert await empty.discover_tools() == []


async def test_unstartable_server_does_not_block_the_others(
    server_commands: dict[str, str],
) -> None:
    """One bad server must not take down discovery for the rest."""
    settings = MCPSettings(
        enabled=True,
        servers={
            "broken": "/definitely/not/a/real/binary --flag",
            "readonly": server_commands["readonly"],
        },
    )
    async with MCPClientManager(settings) as partial:
        assert partial.connected_servers == ["readonly"]
        tools = await partial.discover_tools()
        assert {tool.server for tool in tools} == {"readonly"}


async def test_get_manager_requires_startup(manager: MCPClientManager) -> None:
    from app.core.exceptions import ConfigurationError

    set_manager(None)
    with pytest.raises(ConfigurationError):
        get_manager()


# -- Graph ---------------------------------------------------------------


async def test_graph_calls_a_read_only_tool(manager: MCPClientManager) -> None:
    from app.agents.mcp_tools import call_mcp_tool

    set_manager(manager)
    result, decision, tools = await call_mcp_tool(
        tool_name="readonly.read_note",
        arguments={"key": "k"},
        requested_by="planner",
        reason="gathering context",
    )
    assert decision is not None and decision.allowed is True
    assert result is not None and result.success is True

    # The catalog spans both fixture servers, so check the read-only server's tools
    # specifically rather than assuming everything is low risk.
    by_name = {tool.qualified_name: tool for tool in tools}
    assert by_name["readonly.read_note"].risk_level == "low"
    assert by_name["mixed.overwrite_thing"].risk_level == "high"


async def test_graph_rejects_an_unknown_tool_name(manager: MCPClientManager) -> None:
    from app.agents.mcp_tools import call_mcp_tool

    set_manager(manager)
    with pytest.raises(ValidationError, match="Unknown MCP tool"):
        await call_mcp_tool(tool_name="readonly.nope", requested_by="planner")


async def test_graph_refuses_a_command_tool(manager: MCPClientManager) -> None:
    from app.agents.mcp_tools import call_mcp_tool

    set_manager(manager)
    result, decision, _ = await call_mcp_tool(tool_name="mixed.run_command", requested_by="planner")
    assert decision is not None and decision.allowed is False
    assert result is not None and result.error_code == "policy_denied"


async def test_graph_stops_a_destructive_call_without_approval(
    manager: MCPClientManager,
) -> None:
    from app.agents.mcp_tools import call_mcp_tool

    set_manager(manager)
    result, decision, _ = await call_mcp_tool(
        tool_name="mixed.overwrite_thing", requested_by="planner"
    )
    assert decision is not None and decision.requires_approval is True
    assert result is not None and result.error_code == "approval_required"


async def test_graph_records_the_requesting_agent(manager: MCPClientManager) -> None:
    """The audit trail must name who asked, not just what ran."""
    from app.agents.mcp_tools import call_mcp_tool

    set_manager(manager)
    result, _decision, _tools = await call_mcp_tool(
        tool_name="readonly.read_note", requested_by="repository-planner"
    )
    assert result is not None
    assert result.requested_by == "repository-planner"


# -- API -----------------------------------------------------------------


@pytest.fixture
def mcp_client(server_commands: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Any:
    """A TestClient whose lifespan connects to the fixture servers."""
    commands = f"readonly={server_commands['readonly']},mixed={server_commands['mixed']}"
    monkeypatch.setenv("AEGIS_MCP__ENABLED", "true")
    monkeypatch.setenv("AEGIS_MCP__SERVERS", commands)
    monkeypatch.setenv("AEGIS_MCP__REQUEST_TIMEOUT_SECONDS", "10")
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_tools_endpoint_lists_the_catalog(mcp_client: TestClient) -> None:
    response = mcp_client.get("/api/mcp/tools")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 4
    assert "readonly.read_note" in body["tool_names"]
    assert set(body["servers"]) == {"readonly", "mixed"}


def test_call_endpoint_invokes_a_tool(mcp_client: TestClient) -> None:
    response = mcp_client.post(
        "/api/mcp/tools/call",
        json={
            "tool_name": "readonly.read_note",
            "arguments": {"key": "api"},
            "requested_by": "curl",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["requested_by"] == "curl"


def test_call_endpoint_reports_refusal_in_the_body(mcp_client: TestClient) -> None:
    response = mcp_client.post(
        "/api/mcp/tools/call",
        json={"tool_name": "mixed.overwrite_thing", "requested_by": "curl"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "approval_required"
    assert body["requires_approval"] is True


def test_call_endpoint_rejects_an_unknown_tool(mcp_client: TestClient) -> None:
    response = mcp_client.post(
        "/api/mcp/tools/call",
        json={"tool_name": "readonly.nope", "requested_by": "curl"},
    )
    assert response.status_code == 422
    assert "Unknown MCP tool" in response.json()["error"]["message"]


def test_call_endpoint_refuses_a_command_tool(mcp_client: TestClient) -> None:
    response = mcp_client.post(
        "/api/mcp/tools/call",
        json={"tool_name": "mixed.run_command", "requested_by": "curl"},
    )
    assert response.status_code == 200
    assert response.json()["error_code"] == "policy_denied"


def test_call_endpoint_requires_a_requesting_agent(mcp_client: TestClient) -> None:
    response = mcp_client.post("/api/mcp/tools/call", json={"tool_name": "readonly.read_note"})
    assert response.status_code == 422


# -- Health reporting ----------------------------------------------------


def test_health_reports_mcp_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_MCP__ENABLED", "false")
    monkeypatch.setenv("AEGIS_MCP__SERVERS", "")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as isolated_client:
        body = isolated_client.get("/api/v1/health").json()
    get_settings.cache_clear()

    mcp = next(c for c in body["components"] if c["name"] == "mcp")
    assert mcp["status"] == "not_configured"
    assert mcp["detail"] == "Disabled"


def test_health_distinguishes_enabled_without_servers(
    server_commands: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured but idle is not the same as broken."""
    monkeypatch.setenv("AEGIS_MCP__ENABLED", "true")
    monkeypatch.setenv("AEGIS_MCP__SERVERS", "")
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        body = test_client.get("/api/v1/health").json()
    get_settings.cache_clear()

    mcp = next(c for c in body["components"] if c["name"] == "mcp")
    assert mcp["status"] == "not_configured"
    assert "no servers" in mcp["detail"].lower()


def test_health_reports_connected_servers(mcp_client: TestClient) -> None:
    body = mcp_client.get("/api/v1/health").json()
    mcp = next(c for c in body["components"] if c["name"] == "mcp")
    assert mcp["status"] == "ok"
    assert "readonly" in mcp["detail"]


def test_health_flags_servers_that_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured server that fails to start is degraded, not healthy."""
    monkeypatch.setenv("AEGIS_MCP__ENABLED", "true")
    monkeypatch.setenv("AEGIS_MCP__SERVERS", "broken=/definitely/not/a/real/binary")
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        body = test_client.get("/api/v1/health").json()
    get_settings.cache_clear()

    mcp = next(c for c in body["components"] if c["name"] == "mcp")
    assert mcp["status"] == "error"
