# Aegis

**Autonomous AI DevOps Engineer.**

Aegis is an agent that analyses a software repository, produces a deployment
plan, deploys it through audited MCP tools, verifies the result, and investigates
failures — escalating to a human before it does anything destructive.

> **Status: Stage 4 of 18 — first working agent.**
> A LangGraph planning agent is live: it turns a natural-language DevOps request
> into a structured execution plan via `POST /api/agent/plan`. Plans name the
> tools they would use but nothing is executed yet. No MCP server, no GitHub or
> cloud calls, and no credentials are required.
> See [`docs/roadmap.md`](docs/roadmap.md) for what comes next.

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
│       ├── services/  business logic incl. the LLM interface
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
| `GET`   | `/health`             | Unversioned alias for containers |
| `GET`   | `/docs`               | OpenAPI / Swagger UI             |

Health and agent routes use different prefixes on purpose: health is a stable,
committed surface under `/api/v1`, while the agent API is still pre-1.0 and sits
at `/api`. Agent routes move under the versioned prefix once the response shape
settles.

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
    { "name": "mcp",      "status": "not_configured",  "detail": "Not required yet" },
    { "name": "database", "status": "not_configured",  "detail": "Not required yet" },
    { "name": "cache",    "status": "not_configured",  "detail": "Not required yet" }
  ]
}
```

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

## Configuration

Copy `.env.example` to `.env` and adjust:

```bash
cp .env.example .env
```

**Nothing is required.** The backend boots with no configuration at all, and no
external service is contacted at startup. Every variable is optional.

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
| `AEGIS_GITHUB__*`| GitHub token, org, API URL                 | No                 |
| `AEGIS_MCP__*`   | MCP enable flag, servers, approval policy   | No                 |
| `AEGIS_DATABASE__*` | PostgreSQL URL and pool settings        | No                 |
| `AEGIS_REDIS__*` | Redis URL and pool settings                | No                 |
| `AEGIS_AWS__*`   | Region, account, profile or access keys    | No                 |

Sections are read and validated but perform **no I/O**. Setting
`AEGIS_DATABASE__URL` only changes what the health endpoint reports; no
connection is opened. boto3 and the GitHub and MCP clients are not installed.

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
correlation-ID middleware, the error envelope, and the planning graph and
endpoint. It runs fully offline: the planner is deterministic and nothing calls
out to a provider or a cloud API.

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

Add a licence (MIT is a reasonable default for a student project).#   A e g i s 
 
 