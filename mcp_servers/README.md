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
| `github/`     | `get_repository`, `list_branches`, `list_commits`, `list_issues`, `list_pull_requests`, `list_files`, `get_file_contents` | Shipped (read-only) |
| `docker/`     | `docker_available`, `list_images`, `build_image`, `start_container`, `stop_container`, `container_status`, `container_health`, `container_logs` | Shipped (writes locally) |
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

### The GitHub server

`github/server.py` is read-only by construction: it exposes seven tools and none of
them mutates anything. A test asserts this against the server's real tool list
rather than trusting the docstring, because a docstring is not a control.

It talks to the REST API with `urllib` from the standard library, so it runs anywhere
Python does without an extra dependency.

```bash
export AEGIS_GITHUB__TOKEN=github_pat_...      # a read-only token is enough
export AEGIS_MCP__SERVERS=github=python ../mcp_servers/github/server.py
export AEGIS_MCP__FORWARD_ENVIRONMENT=AEGIS_GITHUB__TOKEN,AEGIS_GITHUB__API_URL
```

Anticipated failures raise the SDK's `ToolError`, so the caller receives an
actionable message ("AEGIS_GITHUB__TOKEN is not set") instead of a generic
"error executing tool". A plain exception would be treated as a crash and its
message withheld — which, for the one error every operator will hit first, would be
no message at all.

### Credential rules

The token is read from the environment and used in exactly one place: an
`Authorization` header. It is never an argument, never in a result, and never in an
error — every string leaving the module passes through `_scrub`, which covers the
case where GitHub echoes a credential back inside an error body.

The backend reaches GitHub only by asking for a tool by name. Its workflow module
makes no HTTP request and never reads or stores a credential, so there is nowhere
for the token to leak on that side either. (Settings do hold an `api_url` and the
token, as `SecretStr`, because that is what gets forwarded to this server.)

### The Docker server

`docker/server.py` exposes eight tools over the Docker CLI. Five are read-only
and run unattended; two change local state and need approval; one is destructive.

| Tool                | Risk   | Approval | Why |
| ------------------- | ------ | -------- | --- |
| `docker_available`  | low    | no       | Reports CLI, daemon, version, context root |
| `list_images`       | low    | no       | Local images with id, tag, size |
| `container_status`  | low    | no       | Status, exit code, restart count, health |
| `container_health`  | low    | no       | Health check status, last output, failing streak |
| `container_logs`    | low    | no       | Capped tail of stdout and stderr |
| `build_image`       | medium | **yes**  | Executes the Dockerfile's `RUN` steps |
| `start_container`   | medium | **yes**  | Starts a container on the host |
| `stop_container`    | high   | **yes**  | Terminates a running workload |

How it avoids being a shell:

- **Argument lists, `shell=False`, always.** There is no tool that takes a
  command string, and `start_container` appends the image *last* with no
  parameter after it, so `sh -c ...` cannot be smuggled in as an override.
- **Names cannot become flags.** An image or container name starting with `-` is
  rejected. Values reach `argv`, where Docker would otherwise read `--privileged`
  as an option.
- **Build contexts are confined** to `AEGIS_DOCKER__CONTEXT_ROOT`, resolved
  *before* the containment check so a symlink cannot escape it.
- **No health command.** `--health-cmd` would be execution inside a container, so
  the capability is absent rather than restricted. Health comes from the image's
  own `HEALTHCHECK`, and an image without one reports `no_healthcheck` — never
  `healthy`.
- **The child environment is an allowlist.** Only `PATH`, `HOME`, the Docker
  connection variables and `AEGIS_DOCKER__*` are inherited, so a build step
  cannot read the backend's LLM or GitHub credentials out of the environment.
- **Bounded capture.** Output is read incrementally and capped; a timed-out
  command has its entire process group killed, so it cannot wedge the server.
- **Logs are capped** in lines and bytes, with `truncated` set so the model knows
  it is reading a fragment. The *tail* is kept, because that is where a crash is.

### Tool naming vs. the policy

The container tool is `start_container`, not `run_container`. Aegis' policy
refuses any tool whose name matches its command-execution pattern, so
`run_container` would be rejected before it ever ran. Rather than carve out an
exception and weaken the guarantee that no tool name can grant shell execution,
the obvious name was given up. A test asserts the refusal still holds.

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
    server.py     # read-only GitHub tools over the REST API
  docker/
    server.py
    README.md
```

Note that the SDK class is `mcp.server.mcpserver.MCPServer`; there is no
`FastMCP` in the current SDK.

A shared helper module for common concerns (error types, approval metadata,
logging) will live in `mcp_servers/common/` once a second server exists —
extracting it earlier would be guesswork.