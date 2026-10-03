# Aegis

**Autonomous AI DevOps Engineer.**

Aegis is an agent that analyses a software repository, produces a deployment
plan, deploys it through audited MCP tools, verifies the result, and investigates
failures — escalating to a human before it does anything destructive.

> **Status: Stage 5 of 18 — MCP client layer.**
> Six LangGraph workflows are live. The planning agent turns a natural-language
> DevOps request into a structured execution plan via `POST /api/agent/plan`, the
> repository analyzer profiles a **local** repository via
> `POST /api/repository/analyze`, the MCP layer discovers and invokes tools on
> configured servers via `GET /api/mcp/tools` and `POST /api/mcp/tools/call`, and
> > `POST /api/github/repository/analyze` profiles a **GitHub** repository and scores
> its deployment readiness, and `POST /api/docker/deploy` builds, runs, health-checks
> and logs a container on the **local** Docker daemon. The read-only paths execute
> nothing; the Docker path changes only the local daemon, and every build and run
> requires explicit human approval through a deny-by-default policy. Nothing is
> pushed to a registry and nothing reaches AWS. See
> [`docs/roadmap.md`](docs/roadmap.md) for what comes next.

## Stack

| Layer      | Technology                                          |
| ---------- | --------------------------------------------------- |
| Frontend   | React 19, Vite 7, TypeScript 5.9                   |
| Backend    | Python 3.11+, FastAPI, Pydantic v2, Uvicorn         |
| Agent core | LangGraph 1.x (optional `agent` extra)             |
| Tooling    | Python MCP SDK (declared, not yet installed)        |
| Storage    | PostgreSQL- and Redis-compatible design, both optional |
| Dev env    | Docker, Docker Compose                              |
| Config     | Environment variables via `.env`                    |

LangGraph lives in an optional `agent` extra so a fresh install stays fast while
the rest of the agent is still being written:

```bash
pip install -e ".[agent,dev]"   # required to run the planning agent
```

## Quick start

Requires Node 20+, Python 3.11+, and (optionally) [uv](https://docs.astral.sh/uv/).

```bash
git clone <your-fork-url> aegis && cd aegis

make setup            # installs backend + frontend dependencies, creates .env
make dev-backend      # terminal 1 → http://localhost:8000
make dev-frontend     # terminal 2 → http://localhost:5173
```

Open <http://localhost:5173>. The dashboard polls the backend and shows its
health; the backend is proxied so both share one origin and there is no CORS
setup to do.

### Without make

```bash
# backend
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[agent,dev]"   # [agent] adds LangGraph, needed for /api/agent/plan
uvicorn app.main:app --reload --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run dev
```

### With Docker

```bash
cp .env.example .env
docker compose up --build
```

- Dashboard: <http://localhost:8080>
- API docs (Swagger): <http://localhost:8000/docs>

Postgres and Redis are available but not started:
`docker compose --profile infra up --build`.

## Project layout

```
aegis/
├── frontend/          React + Vite dashboard
├── backend/           FastAPI service and agent runtime
│   └── app/
│       ├── api/       routers, error handling
│       ├── core/      settings, logging, middleware, exceptions
│       ├── models/    data contracts
│       ├── services/  LLM interface, repository analysis
│       └── agents/    LangGraph workflows
├── docs/              architecture and roadmap
├── docker/            nginx config and Postgres init
├── docker-compose.yml
├── Makefile
└── .env.example
```

## API

| Method  | Path                  | Purpose                          |
| ------- | --------------------- | -------------------------------- |
| `GET`   | `/`                   | Service banner                   |
| `GET`   | `/api/v1/health`      | Aggregate health + components    |
| `GET`   | `/api/v1/health/live` | Liveness probe                   |
| `POST`  | `/api/agent/plan`     | Generate a DevOps execution plan |
| `POST`  | `/api/repository/analyze` | Profile a local repository    |
| `GET`   | `/api/mcp/tools`      | List tools from MCP servers          |
| `POST`  | `/api/mcp/tools/call` | Invoke one MCP tool                  |
| `POST`  | `/api/github/repository/analyze` | Profile a GitHub repository and assess deployment readiness |
| `POST`  | `/api/docker/deploy` | Build, run, health-check and log a container locally |
| `POST`  | `/api/deployment/plan` | Turn a GitHub repository into an ordered deployment plan |
| `GET`   | `/health`             | Unversioned alias for containers |
| `GET`   | `/docs`               | OpenAPI / Swagger UI             |

Health and agent routes use different prefixes on purpose: health is a stable,
committed surface under `/api/v1`, while the agent APIs are still pre-1.0 and sit
at `/api`. Agent routes move under the versioned prefix once the response shapes
settle.

`/api/v1/health` reports the state of each subsystem honestly, so subsystems
that are not built yet appear as `not_configured`:

```json
{
  "status": "ok",
  "service": "Aegis Backend",
  "version": "0.1.0",
  "environment": "local",
  "uptime_seconds": 12.4,
  "stage": "stage-4-planning-agent",
  "components": [
    { "name": "api",      "status": "ok",              "detail": "Serving requests" },
    { "name": "graph",    "status": "ok",              "detail": "Planning graph loaded (no tools executed)" },
    { "name": "mcp",      "status": "not_configured",  "detail": "Disabled; no servers configured" },
    { "name": "database", "status": "not_configured",  "detail": "Not required yet" },
    { "name": "cache",    "status": "not_configured",  "detail": "Not required yet" }
  ]
}
```

## The repository analyzer

`POST /api/repository/analyze` performs a static, read-only inspection of a
repository **on the machine running the backend**. There is no GitHub access
yet.

```bash
curl -sS -X POST http://localhost:8000/api/repository/analyze \
  -H 'Content-Type: application/json' \
  -d '{"path":"/srv/aegis/repos/my-app"}'
```

The workflow is five nodes, with path containment as the first one:

```
START → resolve_target → scan_repository → detect_stack → build_profile → END
```

It is callable on its own from Python, without HTTP:

```python
from app.agents.repository_analysis import analyze_repository_path

profile = analyze_repository_path("/srv/aegis/repos/my-app")
print(profile.primary_language, profile.frontend_framework, profile.entry_points)
```

The `RepositoryProfile` reports languages and the primary language, frontend and
backend frameworks, package manager and package files, Dockerfile and
docker-compose presence, test framework and test files, likely entry points,
environment files, database usage, CI/CD systems, and README presence. A `notes`
field records anything ambiguous, and `truncated` reports whether a scan limit
was hit.

### Treating a repository as untrusted input

A repository is attacker-controlled data, so the analyzer is written defensively:

| Control                | Behaviour                                                  |
| ---------------------- | ---------------------------------------------------------- |
| Path containment       | Resolved path must stay inside `AEGIS_REPOSITORY_ROOT`     |
| No code execution      | Nothing is imported, `eval`'d, or run; only text is read   |
| Symlinks skipped       | A link cannot redirect the scan outside the root           |
| Size caps              | Files over 512 KB are skipped; 5000 files / depth 12 max   |
| Noise pruned           | `node_modules`, `.git`, `.venv`, `dist` are not walked      |
| Malformed manifests    | Unparseable JSON/TOML is ignored, never fatal               |
| `.env` never opened    | Environment files are listed by name only, never read      |
| Read-only              | Nothing is written; no lock files, no caches                |

`.env` handling is deliberate: naming them is useful for planning, but reading
them would put secrets into an HTTP response and the logs.

`AEGIS_REPOSITORY_ROOT` defaults to the backend working directory, so the safe
answer is the default. Point it at a directory only when you intend to grant
access to it.

## The planning agent

`POST /api/agent/plan` runs a LangGraph workflow and returns a plan that has been
drafted and checked, but **not executed**.

```bash
curl -sS -X POST http://localhost:8000/api/agent/plan \
  -H 'Content-Type: application/json' \
  -d '{"request":"Deploy my Node.js application from the main branch."}'
```

The graph is four nodes, each a plain function over a typed state:

```
START → request_analyzer → planner → plan_validator → END
```

- **`request_analyzer`** classifies the request (task type, stack, branch,
  environment) and records its confidence plus anything ambiguous.
- **`planner`** calls the LLM service and returns the structured plan.
- **`plan_validator`** checks the plan is internally consistent — sequential
  steps, every tool declared, high risk implies human approval, verification
  present — and fails the request with a `validation_error` if not.

The plan carries an `objective`, `task_type`, `assumptions`, `required_tools`,
ordered `steps`, `risk_level`, `requires_human_approval`, and
`expected_verification`. Steps carry a `tool` **name**; nothing calls it yet.

### Changing the model or provider

Provider code lives behind one interface, `PlanningLLM`, in
`backend/app/services/llm.py`. The graph and API only ever call
`draft_plan(request, analysis)`.

The default `HeuristicPlanningLLM` is deterministic and makes no network call,
so the project runs and tests pass with no API key. To use a real provider, add a
class implementing `PlanningLLM` and return it from `get_planning_llm()`. No other
file changes.

### Errors

Every error — validation failure, unknown route, or an unhandled exception —
uses one envelope, so a client needs a single parsing path:

```json
{
  "error": {
    "code": "not_found",
    "message": "Deployment dep_123 was not found.",
    "details": { "resource": "deployment", "id": "dep_123" },
    "correlation_id": "3f2a1c8e-9b7d-4f21-8c6a-1d5e7f9a0b3c"
  }
}
```

`code` is stable and safe to branch on. Stack traces are never returned; they go
to the log, correlated by `correlation_id`.

## MCP integration

Aegis reaches external systems through
[Model Context Protocol](https://modelcontextprotocol.io) servers. The backend
launches them as local `stdio` subprocesses, discovers their tools at startup,
and runs the tool graph over them.

No external servers are wired up yet. A safe demo server ships with the repo so the
client layer can be exercised for real, without credentials.

### Enabling it

```bash
# backend/.env
AEGIS_MCP__ENABLED=true
AEGIS_MCP__SERVERS=demo=python ../mcp_servers/demo_server.py
```

`servers` is a comma-separated list of `name=command` pairs. Commands are parsed
with shell-style quoting but **executed without a shell**, so `sh`, `&&` and pipes
are unavailable. Any path containing spaces must be quoted:

```bash
AEGIS_MCP__SERVERS=demo=python "/opt/my projects/mcp_servers/demo_server.py"
```

Sessions are opened once during application startup and closed on shutdown, rather
than per request, so a tool call never pays process-spawn cost and no server
process outlives the backend.

### Trying it

```bash
curl -s localhost:8000/api/mcp/tools \
  | jq '.tools[] | {name: .qualified_name, risk: .risk_level}'
curl -s localhost:8000/api/mcp/tools/call \
  -H 'content-type: application/json' \
  -d '{"tool_name": "demo.get_system_info", "requested_by": "curl"}' | jq
```

### The tool graph

```
START → discover_tools → build_call_request → evaluate_policy → invoke_tool → END
```

The steps are separate nodes because they have different failure modes and
different security relevance. Only `invoke_tool` can cause a side effect, and
everything before it is metadata work that can be inspected and tested without a
server running. `discover_tools` re-reads the registry on every run, so a call is
always planned against the tools that exist right now rather than a stale snapshot.

### Every call passes the policy

`backend/app/services/mcp_policy.py` decides whether a call may proceed. It is
deny-by-default, and refusal is the answer whenever a signal is ambiguous — a
policy that guesses wrong in the permissive direction is a breach, not a usability
issue.

1. **The tool must have been discovered.** An invented tool name never reaches a
   server.
2. **Command-execution tools are refused outright**, whatever the server advertises
   about them. A compromised or hostile server must not be able to offer Aegis a
   shell. Argument keys are checked at any nesting depth, so
   `{"nested": {"command": "..."}}` is refused too.
3. **Risk comes from the server's own annotations**, and the risk in a request is
   metadata, not authority: an agent cannot declare its own destructive call to be
   low risk.
4. **Anything above the approval threshold needs a human.** Read-only tools run
   unattended; a destructive tool is refused until `approval_granted` is set, with
   `approval_reference` recorded in the audit log.

Refusals come back in the response body with `success: false` and an `error_code`
of `policy_denied` or `approval_required`, because a refusal is a recorded outcome
rather than a fault in the request. There is no endpoint or flag that skips the
policy — it is a property of the architecture, not of a caller's good behaviour.

### Every server is treated as untrusted input

A server's tool list, descriptions and annotations are data, not instructions. It
cannot grant itself permission by claiming a tool is read-only, and it cannot
widen its own blast radius. Only tools discovered from configured servers are
reachable, and only configured commands are ever launched.

### GitHub

`mcp_servers/github/server.py` exposes read-only access to a GitHub repository:
repository metadata, branches, commits, issues, pull requests, the file tree, and
file contents. It has **no** tool that creates a commit, branch or pull request, and
a test asserts that against the server's real tool list rather than trusting the
docstring.

The backend has no GitHub HTTP client. The analysis workflow asks for a tool by name
and the MCP layer does the rest, so rate limiting, caching and transport changes are
one layer's problem:

```bash
# backend/.env
AEGIS_GITHUB__TOKEN=github_pat_...          # read-only is sufficient
AEGIS_MCP__SERVERS=github=python ../mcp_servers/github/server.py
AEGIS_MCP__FORWARD_ENVIRONMENT=AEGIS_GITHUB__TOKEN,AEGIS_GITHUB__API_URL
```

`AEGIS_MCP__FORWARD_ENVIRONMENT` is required because the MCP SDK deliberately
inherits only a small allowlist (`PATH`, `HOME` and friends). Naming the variables
keeps forwarding opt-in: adding a server cannot silently hand it every secret the
backend holds. The token travels through the subprocess environment rather than
argv, which matters because argv is visible to any process on the host via `ps`.

#### Where the token can and cannot appear

| Location                       | Token |
| ------------------------------ | ----- |
| `Authorization` request header | Yes — this is the only place it is used |
| Backend settings `repr()`, logs, `/api/v1/health` | No — `SecretStr` masks it, and `safe_summary()` reduces it to a boolean |
| Forwarded subprocess env       | Yes — named explicitly, values never logged |
| Tool arguments, tool results, API responses | No |
| Server error messages          | No — every string leaving the server passes a scrubber |
| GitHub MCP workflow module     | No — it never reads or stores a credential |

A test asserts each of those, including that scrubbing works when GitHub echoes the
token back inside an error body.

#### Analysing a repository

```bash
curl -s localhost:8000/api/github/repository/analyze \
  -H 'content-type: application/json' \
  -d '{"owner":"acme","repository":"checkout-service","include_issues":true}' | jq
```

```
START → fetch_repository → inspect_files → detect_stack
      → inspect_commits → inspect_issues → assess_readiness → END
```

`owner` and `repository` are rejected if they contain a slash or a dot segment,
because both become URL path segments in the API request and a caller must not be
able to address a path other than the repository they named.

Stack detection is **shared** with the local analyzer. `detect_stack` reads only
paths and manifest text, so a synthetic inventory built from remote paths produces
the same profile without a second detector drifting out of step. That is also why
manifest extraction is public (`extract_dependencies`) rather than duplicated.

A failure to read commits or issues is recorded in `profile.notes` and the analysis
still completes — losing context should not lose the profile. Issues you did not
ask for are reported as `issues_inspected: false`, not as zero issues, so
"not looked at" is never confused with "nothing open".

#### Deployment readiness

`assess_readiness` turns the profile into a score out of 100, a list of blockers, and
a list of named checks showing which facts moved the score. An unexplained number
would be impossible to act on or to argue with.

Blockers are the things that make deployment unsafe or impossible: an archived
repository, no Dockerfile, no CI, no tests. Warnings are risks worth a human's
attention: no lockfile, a truncated file listing, a large issue backlog, multiple
languages. `ready` is true only when there are no blockers, so a high score with a
blocker still reports not ready.

The score is a heuristic and says so in `readiness.notes`.

### Docker

`mcp_servers/docker/server.py` exposes eight tools over the Docker CLI: availability,
image listing, build, start, stop, status, health and logs. Five are read-only. Two
change local state and one is destructive, and all three require approval.

```bash
# backend/.env
AEGIS_MCP__SERVERS=docker=python ../mcp_servers/docker/server.py
AEGIS_MCP__FORWARD_ENVIRONMENT=AEGIS_DOCKER__CONTEXT_ROOT
AEGIS_DOCKER__CONTEXT_ROOT=../examples
```

The container tool is named `start_container`, not `run_container`. Aegis' policy
refuses any tool whose name matches its command-execution pattern, so
`run_container` would be rejected outright. Rather than add an exception and weaken
the guarantee that no tool name can ever grant shell execution, the more obvious
name was given up. A test asserts the refusal still holds, so the reasoning cannot
rot.

#### What the Docker server deliberately cannot do

| Capability | Why not |
| ---------- | ------- |
| Run a command in a container | `start_container` appends the image last and exposes no trailing argument, so an entrypoint cannot be overridden. That is what keeps `docker run IMAGE sh -c ...` unreachable. |
| Set a health command | `--health-cmd` is execution inside a container. Health comes from the image's own `HEALTHCHECK`; an image with none reports `no_healthcheck`, never `healthy`. |
| Mount a volume or pass env | Both are ways to hand a container the host's filesystem or credentials. The tests whitelist the entire parameter surface, so adding one fails the build rather than shipping. |
| Pass build args | A build argument is a reliable way to bake a secret into an image layer. |
| Pass environment variables to the build | The child's environment is an allowlist — `PATH`, `HOME`, Docker connection settings and `AEGIS_DOCKER__*` only. A Dockerfile's `RUN` step cannot read the backend's LLM or GitHub credentials. |

Beyond that: names starting with `-` are rejected so they cannot be read as Docker
flags; build contexts are confined to `context_root` with symlinks resolved *before*
the containment check; output is read incrementally and capped; a timed-out command
has its whole process group killed; and logs are capped in lines and bytes with
`truncated` reported.

#### Deploying the sample application

`examples/sample_app` is a dependency-free HTTP service with a `HEALTHCHECK`, used by
the integration tests against a real daemon:

```bash
curl -s localhost:8000/api/docker/deploy \
  -H 'content-type: application/json' \
  -d '{"repository_path":"../examples/sample_app","image":"aegis-sample:dev",
       "container_name":"aegis-sample","ports":["8080:8000"],
       "approve":true,"approval_reference":"your-name"}' | jq
```

```
START → inspect_repository → build_image → start_container → check_health → collect_logs → END
```

`succeeded` is true only when the container started **and** reported healthy, so a
container that is merely running is not reported as a working deployment. Without
`approve: true` the build and run stages are refused and say so, rather than being
quietly skipped. Use `dry_run: true` to inspect without building anything.

### Deployment planning

`POST /api/deployment/plan` turns a GitHub repository into an ordered deployment
plan. It combines three things that are usually three separate agents and three
inconsistent answers:

| Phase | What it produces |
| ----- | ---------------- |
| Repository analysis | Stack, tests, Dockerfile, env templates, readiness |
| DevOps planning | Build, test, deployment, health-check and rollback strategies |
| Risk assessment | Risks, approval requirements, blocked steps |

```bash
curl -s 'localhost:8000/api/deployment/plan?format=markdown' \
  -H 'content-type: application/json' \
  -d '{"owner":"expressjs","repository":"express"}' | less
```

The response carries **both** representations: a machine-readable `plan` object and
a `summary_markdown` rendering, produced from the same fields so they cannot
disagree. Add `?format=markdown` to get the rendered form alone as `text/plain`.

#### Decisions come from the profile

| Condition | Decision |
| --------- | -------- |
| Dockerfile present | Build and deploy via Docker |
| No Dockerfile | Recommend generating one — **never modify the repository** |
| Tests present | Run the suite as a blocking gate |
| No tests | Testing marked *limited*, never as a pass |
| Required env var unresolved | Deployment **blocked** until resolved |

Two of these are worth stating plainly, because both are places where a planner
could quietly produce a confident wrong answer:

**A missing Dockerfile produces a recommendation, not an edit.** The plan says
which Dockerfile to add and why. Writing one is a separate, reviewed action — a
planner that mutates the repository cannot be inspected before it changes anything.

**A placeholder is not a value.** `.env.example` ships with `changeme` and
`your-key-here`, and those are treated as *unresolved*, which blocks the
deployment:

```
DATABASE_URL=changeme          -> unresolved (placeholder) -> blocks
PORT=3000                      -> resolved
DEBUG=                         -> unresolved (empty)       -> blocks
```

Resolved values are never echoed back. A plan reports *that* a variable is
satisfied and *where* it was declared, never its contents.

#### Blocking is contagious

If the build is blocked because there is no Dockerfile, the test step that needs
the image and the run step that needs the image are blocked too — each with its
own reason. Marking only the first step would invite someone to skip ahead.

### Adding a server

See [`mcp_servers/README.md`](mcp_servers/README.md) for the contract each server
must follow.

## Configuration

Copy `.env.example` to `.env` and adjust:

```bash
cp .env.example .env
```

**Nothing is required.** The backend boots with no configuration at all, and no
external service is contacted at startup. Every variable is optional.

### Repository analysis

`AEGIS_REPOSITORY_ROOT` sets the only directory tree the analyzer may read. It
defaults to the backend working directory, so the default denies access to
anything else. Set it explicitly to grant access to a directory of checkouts:

```bash
AEGIS_REPOSITORY_ROOT=/srv/aegis/repos
```

A requested path is resolved and then must still land inside that root;
otherwise the request fails with `403 permission_denied`.

### Naming

Backend variables use the `AEGIS_` prefix and nest with a double underscore:

```
AEGIS_LLM__API_KEY=...        →  Settings.llm.api_key
AEGIS_DATABASE__URL=...       →  Settings.database.url
```

### Available sections

| Section          | Purpose                                    | Client implemented |
| ---------------- | ------------------------------------------ | ------------------ |
| `AEGIS_*`        | Service, HTTP, CORS, logging               | Yes                |
| `AEGIS_LLM__*`   | LLM provider, model, key, timeouts         | No                 |
| `AEGIS_GITHUB__*`| GitHub token, org, API URL (read by the MCP server) | Via MCP   |
| `AEGIS_MCP__*`   | Enable flag, servers, forwarded env, approval policy | Yes |
| `AEGIS_DATABASE__*` | PostgreSQL URL and pool settings        | No                 |
| `AEGIS_REDIS__*` | Redis URL and pool settings                | No                 |
| `AEGIS_AWS__*`   | Region, account, profile or access keys    | No                 |

Sections are read and validated, but most perform **no I/O**. Setting
`AEGIS_DATABASE__URL` only changes what the health endpoint reports; no connection
is opened, and boto3 is not installed. The two exceptions are MCP and GitHub: MCP
launches the servers you configure and discovers their tools at startup, and
GitHub is reached only through the `github` MCP server rather than by a client in
the backend.

### Credentials

- No credential is hardcoded anywhere in the repository, and every placeholder
  in `.env.example` is blank or obviously fake.
- Secrets are typed as `SecretStr`, so they render as `**********` in `repr()`,
  logs, tracebacks and error payloads. Database and Redis URLs are secrets too,
  because a URL commonly embeds a password.
- `.env` is gitignored. Commit `.env.example` instead — it is the template.
- Prefer injecting secrets through the environment or a secret manager
  (AWS Secrets Manager, Vault, GitHub Actions secrets) rather than writing them
  into a file on disk.
- To confirm a credential is present, check `configured_integrations()` on the
  settings object, never read the value back.
- A server launched by MCP receives credentials only if you name the variable in
  `AEGIS_MCP__FORWARD_ENVIRONMENT`. Forwarding is opt-in per variable, and the
  values are never logged — only the names are.

### Logging

```bash
AEGIS_LOG_LEVEL=INFO       # DEBUG | INFO | WARNING | ERROR | CRITICAL
AEGIS_LOG_FORMAT=console   # console for humans, json for aggregators
```

Set `AEGIS_LOG_FORMAT=json` in staging and production. Each line becomes a JSON
object, so method, path, status and duration are queryable rather than buried in
a string:

```json
{"timestamp":"2026-10-02T14:50:21.286Z","level":"INFO","logger":"app.core.middleware",
 "message":"request completed","correlation_id":"23fbfac6-…","method":"GET",
 "path":"/api/v1/health","status_code":200,"duration_ms":2.29,"client_ip":"127.0.0.1"}
```

### Correlation IDs

Every request gets an ID: taken from an inbound `X-Request-ID` when it is
well-formed, otherwise generated as a UUID4. It is:

- bound to a `ContextVar`, so any log line emitted while handling the request
  carries it without threading a parameter through every call;
- echoed on the response as `X-Request-ID`;
- included in error payloads under `error.correlation_id`.

Inbound IDs are validated against `[A-Za-z0-9_-]{1,64}` and replaced when they
do not match, which prevents log injection through the header.

```bash
curl -sD- http://localhost:8000/api/v1/health -H 'X-Request-ID: my-trace-42' | grep -i x-request-id
# x-request-id: my-trace-42
```

To find everything one request did:

```bash
AEGIS_LOG_FORMAT=json uvicorn app.main:app | grep my-trace-42
```

### Client addresses

`AEGIS_TRUST_FORWARDED_HEADERS` defaults to `false`, so the access log records
the socket peer rather than `X-Forwarded-For`, which any client can forge. Enable
it only when the service genuinely sits behind a proxy you control.

## Testing

```bash
make test          # backend pytest + frontend vitest
make smoke         # smoke tests against a running backend (skips if it is down)
make check         # lint + typecheck + test
make lint          # ruff + eslint
make typecheck     # tsc --noEmit
```

The backend suite covers the health contract, configuration, logging, the
correlation-ID middleware, the error envelope, the planning graph and endpoint,
and the repository analyzer — including path traversal, symlink escapes,
read-only guarantees and `.env` redaction.

The analyzer tests generate a small fixture repository into a temporary
directory rather than committing a sample tree, and re-point
`AEGIS_REPOSITORY_ROOT` at it so no test can read the real source tree.

Everything runs fully offline: the planner is deterministic and nothing calls
out to a provider, a cloud API, or GitHub.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — component design, layering
  rules, safety model
- [`docs/roadmap.md`](docs/roadmap.md) — all 18 stages and current status
- [`mcp_servers/README.md`](mcp_servers/README.md) — MCP server contract and the
  rules every future server must follow
- [`backend/README.md`](backend/README.md) — backend module reference
- [`frontend/README.md`](frontend/README.md) — frontend module reference
- [`docker/README.md`](docker/README.md) — Docker usage

## Licence

Add a licence (MIT is a reasonable default for a student project).
