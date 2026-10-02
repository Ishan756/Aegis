# MCP Servers

Aegis reaches external systems through
[Model Context Protocol](https://modelcontextprotocol.io) servers. Each server is a
standalone process exposing a narrow set of audited tools; the backend's
`app/services/mcp_manager.py` discovers and invokes them, and
`app/services/mcp_policy.py` decides whether a call may proceed at all.

## Servers

| Server        | Tools it exposes                                     | Stage   |
| ------------- | ---------------------------------------------------- | ------- |
| `demo_server.py` | `get_system_info`, `get_project_files`, `get_project_status` | Shipped |
| `github/`     | read repo, list branches, read file, open PR, read checks   | Stage 9 |
| `docker/`     | build image, push image, inspect container, read logs       | Stage 10 |
| `kubernetes/` | apply manifest, rollout status, pod logs                    | Stage 12 |
| `aws/`        | describe instances, deploy, tail logs, rollback             | Stage 13 |

### The demo server

`demo_server.py` exists so the client layer can be built and tested against
something real without credentials or infrastructure. Every tool it exposes is
annotated `read_only` and none is destructive, so all three are classified low
risk and run without human approval.

It is deliberately incapable of doing damage:

- no shell, `exec` or command tool — and the policy layer would refuse one even if
  a server advertised it
- `get_project_files` is confined to a root directory, skips symlinks, and caps
  depth and result count, so it cannot be used to walk the filesystem
- `get_system_info` returns fixed, non-sensitive fields only; no environment
  variable or credential is ever reported

Point it at a scratch directory with `AEGIS_DEMO_ROOT` rather than at a real
repository.

## Running a server

Servers speak stdio and run as child processes of the backend. In
`backend/.env`:

```bash
AEGIS_MCP__ENABLED=true
AEGIS_MCP__SERVERS=demo=python ../mcp_servers/demo_server.py
```

Commands are parsed with shell-style quoting but executed **without** a shell, so
`sh`, `&&` and pipes are not available. Any path containing spaces must be quoted:

```bash
AEGIS_MCP__SERVERS=demo=python "/opt/my projects/mcp_servers/demo_server.py"
```

To run the demo server by hand:

```bash
python mcp_servers/demo_server.py
```

It prints protocol traffic on stdout and nothing else, so do not add `print`
statements to it.

## Rules each server must follow

1. **Read and write are separate tools.** e.g. `github.read_file` vs
   `github.create_pull_request`. The agent should never mutate state with a
   tool whose name does not say so.
2. **Annotate every tool.** `readOnlyHint` and `destructiveHint` are what Aegis
   reads to assign risk. A tool with no annotation is treated as medium risk,
   because the protocol default is that a tool may do anything.
3. **Destructive tools declare their blast radius.** Anything that deletes,
   rolls back or terminates gets an explicit `destructive: true` marker and
   requires human approval.
4. **No ambient credentials.** Servers read secrets from the environment only;
   no tokens are ever written to disk or returned in a response.
5. **Structured errors.** Return a typed error, never a stack trace, so the
   agent can decide whether to retry or escalate.
6. **Local stdio transport by default.** Servers run as child processes of the
   backend, which keeps permissions scoped to the Aegis process.
7. **No command-execution tools.** Tool names matching `run_command`, `shell`,
   `exec` and similar are refused by Aegis's policy no matter what the server
   advertises. A server that needs to do something is expected to expose a
   narrow tool for that specific action instead.

## Layout

```
mcp_servers/
  demo_server.py  # safe read-only fixtures for development and tests
  README.md
  github/
    server.py     # MCPServer app, tool definitions
    README.md
  docker/
    server.py
    README.md
```

Note that the SDK class is `mcp.server.mcpserver.MCPServer`; there is no
`FastMCP` in the current SDK.

A shared helper module for common concerns (error types, approval metadata,
logging) will live in `mcp_servers/common/` once a second server exists —
extracting it earlier would be guesswork.