# Aegis Backend

FastAPI service that hosts Aegis' HTTP API and the LangGraph agent runtime.

## Layout

| Path                  | Responsibility                                            |
| --------------------- | --------------------------------------------------------- |
| `app/main.py`         | Application factory, middleware, lifespan                 |
| `app/api/`            | Routers, error handlers, FastAPI dependencies             |
| `app/api/routes/`     | One module per API area (`health`, `agent`, `mcp`, `github`, `docker`) |
| `app/core/`           | Settings, logging, request middleware, exception types    |
| `app/models/`         | Pydantic contracts (`health`, `errors`, `planning`, `mcp`, `github`, `docker`, `repository`) |
| `app/services/`       | Business logic, including the LLM provider interface      |
| `app/agents/`         | LangGraph state graphs                                    |
| `tests/`              | Unit and API tests                                        |

## Endpoints

| Method | Path                      | Purpose                          |
| ------ | ------------------------- | -------------------------------- |
| `GET`  | `/`                       | Service banner                   |
| `GET`  | `/api/v1/health`          | Aggregate health + components    |
| `GET`  | `/api/v1/health/live`     | Liveness probe                   |
| `POST` | `/api/agent/plan`         | Generate a DevOps execution plan |
| `POST` | `/api/repository/analyze` | Profile a local repository       |
| `GET`  | `/api/mcp/tools`          | List tools from MCP servers      |
| `POST` | `/api/mcp/tools/call`     | Invoke one MCP tool              |
| `POST` | `/api/github/repository/analyze` | Profile a GitHub repository and score deployment readiness |
| `GET`  | `/api/docker/availability`  | Report whether the Docker daemon is reachable |
| `POST` | `/api/docker/deploy`        | Build, run, health-check and log a local container |
| `POST` | `/api/deployment/plan`      | Plan a deployment from a GitHub repository |
| `POST` | `/api/deployment/execute`   | Run an ordered task list with retries, timeouts and approvals |
| `POST` | `/api/deployment/workflow`  | PLAN → EXECUTE → VERIFY → END, with DEBUG on failure |
| `POST` | `/api/verification/deployment` | Verify a deployed container against seven checks |

Health routes are versioned under `/api/v1`; the agent routes are intentionally
unversioned because the response shapes are still pre-1.0.

## Repository analysis

`app/agents/repository_analysis.py` builds the graph:

```
START → resolve_target → scan_repository → detect_stack → build_profile → END
```

`resolve_target` is the security boundary: it resolves the requested path and
rejects anything outside `settings.repository_root_resolved`, so `..` and
symlink escapes fail before any node reads a file. The remaining nodes scan,
detect, and assemble the `RepositoryProfile` from `app/models/repository.py`.

Detection rules live in `app/services/repository.py` as data (extension tables
and marker tuples), so a new language or framework is a one-line addition.

Runnable without HTTP:

```python
from app.agents.repository_analysis import analyze_repository_path

profile = analyze_repository_path("/srv/aegis/repos/my-app")
```

A repository is untrusted input, so the analyzer never executes or imports
anything, skips symlinks, caps file size and count, prunes `node_modules`,
`.git`, `.venv` and `dist`, ignores malformed manifests, and lists `.env` files
by name only so secrets cannot reach a response or a log.

## The planning agent

`app/agents/planner.py` builds the graph:

```
START → request_analyzer → planner → plan_validator → END
```

State is the typed `PlanningState` (`app/models/planning.py`); each node is a
plain function returning only the keys it changes. The validator's failures
surface as a `422` with the standard error envelope, so a broken plan never
returns 200.

**No tools are executed.** A step's `tool` field names a tool a later stage will
call.

### Swapping the LLM

`app/services/llm.py` defines the `PlanningLLM` protocol:

```python
class PlanningLLM(Protocol):
    def draft_plan(self, request: str, analysis: RequestAnalysis) -> DeploymentPlan: ...
```

The graph and API depend only on that method. The shipped
`HeuristicPlanningLLM` is deterministic and offline, which is why the suite needs
no API key. To add a provider, implement the protocol and return the instance
from `get_planning_llm()` — no other module changes.

## MCP

`app/agents/mcp_tools.py` builds the tool graph:

```
START → discover_tools → build_call_request → evaluate_policy → invoke_tool → END
```

The split is the point. Only `invoke_tool` can cause a side effect; the three nodes
before it are metadata work that can be inspected and tested with no server running.
`discover_tools` re-reads the registry on every run, so a call is always planned
against the tools that exist right now.

Three modules cooperate:

| Module                            | Role                                              |
| --------------------------------- | ------------------------------------------------- |
| `app/services/mcp_manager.py`     | Process lifecycle, discovery, invocation, timeout |
| `app/services/mcp_policy.py`      | Whether a call may proceed at all                 |
| `app/models/mcp.py`               | Tool metadata, request, decision, result contracts |

### Lifecycle

Sessions are opened once in the application lifespan and closed on shutdown, not
per request: each server is a subprocess, so reconnecting per request would pay
process-spawn cost on every call and could leave orphaned children. `get_manager()`
returns the single manager and raises if startup did not run, rather than lazily
building a second set of connections that would leak.

### Policy

Deny-by-default, checked in order: the tool must have been discovered; command-
execution names are refused regardless of what the server claims; an optional
allowlist may narrow access further; argument keys are screened at any nesting
depth; then the approval threshold is applied.

Risk comes from the server's `readOnlyHint` / `destructiveHint` annotations. A
tool with no annotation is medium risk, because the protocol default is that a tool
may do anything. Read-only (low risk) tools run unattended; medium and above are
refused until `approval_granted` is set.

Two details worth keeping in mind when changing this code:

- The risk in a `ToolCallRequest` is metadata, not authority. It is recorded for
  the audit trail but never used to lower the assessed risk.
- `MCPError` is not a timeout. The SDK raises a generic `MCPError` for a read that
  exceeds `read_timeout_seconds`, so the message is matched explicitly; otherwise a
  slow tool would be reported as a malformed call.

### Configuration

```bash
AEGIS_MCP__ENABLED=true
AEGIS_MCP__SERVERS=demo=python ../mcp_servers/demo_server.py
AEGIS_MCP__FORWARD_ENVIRONMENT=AEGIS_GITHUB__TOKEN
```

Commands are parsed with `shlex.split` and executed without a shell, so `sh`, `&&`
and pipes are unavailable and quoting is required for paths containing spaces.

`FORWARD_ENVIRONMENT` names the variables copied from the backend's environment into
each subprocess, because the SDK otherwise inherits only `PATH`, `HOME` and similar.
It is an allowlist of names, not a dump: a variable is forwarded only when listed,
and only the names are ever logged. Keeping it explicit means adding a server to
`servers` cannot silently grant it every secret the backend holds.

## GitHub analysis

`app/agents/github_repository_analysis.py` profiles a repository hosted on GitHub:

```
START → fetch_repository → inspect_files → detect_stack
      → inspect_commits → inspect_issues → assess_readiness → END
```

No module in `app/` makes a GitHub HTTP request. Each node asks the MCP layer for
a tool by name, so this is an ordinary consumer of the tool graph and inherits its
policy, approval and timeout behaviour for free. The token is not read here either:
it is held in settings as a `SecretStr` solely so it can be forwarded to the server.

Two deliberate choices are worth preserving:

- Stack detection is shared with the local analyzer. `detect_stack` only reads paths
  and manifest text, so a synthetic `FileInventory` built from remote paths yields
  the same profile. `extract_dependencies` is public for that reason — duplicating a
  detector is how the two versions start disagreeing.
- A failed optional read is a note, not an abort. If issues or commits cannot be
  fetched, `profile.notes` records it and the profile is still returned. Likewise
  `issues_inspected` distinguishes "not looked at" from "none open", so the field
  never implies coverage that did not happen.

Readiness is scored in `assess_readiness` from profile facts, and every check that
contributes a penalty names itself in `readiness.checks` so the number can be
argued with.

## Docker deployment

`app/agents/docker_deployment.py` runs a repository through the local daemon:

```
START → inspect_repository → build_image → start_container → check_health → collect_logs → END
```

No module in `app/` shells out to Docker, and none builds a command line. Every
stage asks the MCP layer for a tool by name, so it inherits the policy, approval
gate and timeouts for free — which is why `build_image` and `start_container` are
medium risk and gated behind `approve: true` plus an `approval_reference`.

Three behaviours are deliberate and worth preserving:

- **The workflow never approves itself.** `approval_granted` comes from the
  caller's request and nowhere else, so an agent cannot set the flag on its own
  request object and walk past the gate.
- **A refusal is reported, not skipped.** An unapproved deploy returns 200 with
  a `refused` step and a note saying how to proceed, rather than pretending the
  stage did not exist.
- **Success means started *and* healthy.** `succeeded` requires both, and an
  image with no `HEALTHCHECK` is never counted as healthy. A build failure
  short-circuits the remaining stages, recorded as `skipped`, so a deployment
  that never started cannot report itself healthy with no logs.

`dry_run: true` inspects and reports the plan without building or running
anything.

## Execution and verification

Three modules, three separate concerns:

| Module | Responsibility |
| ------ | -------------- |
| `app/agents/execution_engine.py` | Run tasks in order, and record what happened |
| `app/agents/deployment_verification.py` | Decide whether the deployment is actually healthy |
| `app/agents/debug_agent.py` | Explain a failure and propose fixes, applying nothing |

### The executor

`execute_run` takes an ordered `Task` list and runs it sequentially. Sequential
because order encodes causality: nothing that needs an image runs before the
build, and nothing that needs a container runs before the run.

Every attempt becomes an `Action` recording the task, tool, redacted arguments,
status, duration, error code and error class. That record is the audit trail, and
it is written per attempt, so a retried task leaves one entry per try rather than
only the last outcome.

Failures are classified, not just counted:

| Class | Retried | Examples |
| ----- | ------- | -------- |
| `TRANSIENT` | yes | daemon restarting, image pull timeout |
| `PERMANENT` | no | no such image, port already allocated, bad Dockerfile |
| `POLICY` | no | refused by the approval gate |
| `TIMEOUT` | yes, if attempts remain | the tool exceeded its budget |

Anything unrecognised is `PERMANENT`. Defaulting unknown failures to retryable
means a typo in a tool name turns into a loop; a tool that is merely *not known*
to be safe should be treated as unsafe.

Only a **critical** task failure stops the run. That distinction is deliberate:
`stopped` means the plan was halted part-way, which is a different event from a
task that simply failed, and reporting both as "stopped" would hide the fact that
nothing after the failure was ever attempted.

Arguments are redacted before they are recorded. Keys that look like secrets go
entirely; a URL that merely *contains* a credential keeps its scheme and host, so
the trail stays useful:

```
{"password": "hunter2"}                        -> {"password": "***redacted***"}
{"dsn": "postgres://u:p@h/db"}                 -> {"dsn": "***redacted***"}
{"DATABASE_URL": "postgres://u:p@h:5432/db"}   -> {"DATABASE_URL": "postgres://***redacted***@h:5432/db"}
```

### The verifier

Seven checks, always in this order:

| # | Check | Question |
| - | ----- | -------- |
| 1 | `container_exists` | Is there a container at all? |
| 2 | `container_running` | Is it running, and was it OOM-killed? |
| 3 | `port_available` | Is the port published **and** answering? |
| 4 | `health_endpoint` | Does the health path respond? |
| 5 | `health_status_code` | Is the status the one we expected? |
| 6 | `logs_clean` | Do the logs contain a real error? |
| 7 | `dependencies_reachable` | Can declared dependencies be reached? |

Two of these carry more weight than they look like they do.

**Check 3 probes the port.** A mapped port is a Docker promise, not evidence.
A container can publish `8080:8000` and have nothing listening, which passes
every status check and fails every request. The check therefore has to make a
request, not read a mapping.

**Check 6 needs both patterns.** Fatal patterns catch `panic`,
`Traceback (most recent call last)`, `ECONNREFUSED`, `out of memory` and friends.
Benign patterns are checked first and catch `ERROR_CODE=0`, `no errors during
startup`, `Errors logged: 0` — because a verifier that cries wolf on
`ERROR_CODE=0` gets switched off, and a verifier nobody trusts is worse than no
verifier. Go and Node print symbolic errno names, so `ECONNREFUSED` is matched
explicitly; a prose-only pattern list misses it completely.

The result is `SUCCESS`, `WARNING` or `FAILED`, and **`WARNING` is not a pass**.
A check that could not run because a prerequisite was missing is a warning,
because absence of evidence is not evidence of health. A check that does not
apply at all — no declared dependencies — is ignored; conflating the two would
make `WARNING` the outcome of every healthy deployment, and a warning that always
fires is one nobody reads.

Verification is strictly read-only. It cannot restart, rebuild or roll anything
back, because an agent that repairs its own deployment destroys the evidence that
tells you the deployment is broken.

### The Debug Agent is a placeholder

On a stopped execution or a non-`SUCCESS` verification the graph routes to
`app/agents/debug_agent.py`. It classifies the failure, cites the evidence for
each hypothesis, and proposes remediations with `requires_approval` set. It
applies nothing: `applied` is always `False` and `applied_action` is always
`None`.

This is on purpose. A debug agent that restarts containers and reruns builds
will retry indefinitely against a cause it cannot see, and it erases the logs and
exit code you needed. Proposals need a human, which is what `requires_approval`
is for.

### The workflow

```
START → PLAN → EXECUTE → VERIFY → END
                 │         │
                 ▼         ▼
               DEBUG ◄─────┘
```

```
PLAN → EXECUTE → VERIFY → END   clean success
      EXECUTE stopped ─────────▶ DEBUG
      VERIFY WARNING/FAILED ───▶ DEBUG
      dry_run ─────────────────▶ END
```

Only a clean `SUCCESS` ends the run. A dry run ends without verifying anything,
because nothing was deployed — running the verifier anyway would report on a
container that does not exist, which is a false failure rather than a useful one.

## Local development

Requires [uv](https://docs.astral.sh/uv/) (or any Python 3.11+ environment).

```bash
uv venv .venv
uv pip install -e ".[agent,dev]"
uv run --no-project uvicorn app.main:app --reload --port 8000
```

Or without uv:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[agent,dev]"
uvicorn app.main:app --reload --port 8000
```

Configuration is read from environment variables prefixed with `AEGIS_`; see
`.env.example` at the repository root.

## Tests and linting

```bash
pytest            # unit + API tests
ruff check .      # lint
ruff format .     # format
```

## Notes

- LangGraph and the MCP SDK are both required by the agent runtime and live in
  the optional `agent` extra. Installing `-e ".[dev]"` alone will fail to import
  the graphs, so use `-e ".[agent,dev]"`.
- The MCP SDK uses `mcp.server.mcpserver.MCPServer`; there is no `FastMCP` in the
  current release.
- The MCP SDK exposes snake_case attributes (`is_error`, `read_only_hint`) for
  wire fields that are camelCase in the protocol. Code reading them checks both
  spellings.
- PostgreSQL and Redis are not required. If `AEGIS_DATABASE_URL` is set the
  health endpoint reports the database as configured, but no connection is
  opened yet.