# Aegis Roadmap

Eighteen stages, built in order. Each stage should leave the project running and
tests green before the next begins.

Legend: ✅ done · ◔ partial · ⬜ not started

## Stage 1 — Foundation ✅

- [x] Monorepo layout (`frontend`, `backend`, `mcp_servers`, `tests`, `docs`, `docker`)
- [x] FastAPI app with modular package boundaries
- [x] Settings from env (`.env`), structured logging with JSON option
- [x] `GET /api/v1/health` reporting real component state
- [x] React + Vite dashboard: title, system status, backend connection, components
- [x] Dockerfiles and `docker-compose.yml`
- [x] Backend pytest suite, frontend vitest suite, smoke tests
- [x] Verified: backend tests + lint, frontend typecheck + lint + test + build,
      and a live Vite → FastAPI round trip

## Stage 2 — Configuration & secrets ✅

- [x] Secret management boundary (no secrets in code or logs)
- [x] Typed client settings with validation
- [x] Request correlation IDs and structured log fields

## Stage 3 — Persistence ◔

- [ ] SQLAlchemy async engine, session factory, migrations (Alembic)
- [ ] Core tables: deployments, tool_calls, approvals, agent_runs
- [ ] Redis wrapper for hot state and locks

## Stage 4 — Agent foundation ◔

- [x] LLM provider abstraction with a single testable fake
- [x] Structured output schemas for every agent response
- [x] Typed LangGraph state and a first graph: `request_analyzer → planner → plan_validator`
- [x] `POST /api/agent/plan` returning a structured, unexecuted plan
- [x] Repository-analysis agent over a **local path**, returning a typed
      `RepositoryProfile`: languages, frameworks, package manager, Docker, tests,
      entry points, env files, database usage, CI/CD, README
- [x] `POST /api/repository/analyze`, runnable independently of HTTP
- [ ] Feed the repository profile into the planning graph

The provider boundary is the `PlanningLLM` protocol in `app/services/llm.py`. Its
default implementation is deterministic and offline; adding a real provider is
one class plus one factory change.

The analyzer treats the repository as untrusted: no execution, symlinks skipped,
size and depth caps, and a containment root (`AEGIS_REPOSITORY_ROOT`) that a
request cannot escape.

## Stage 5 — Orchestration ◔

- [x] LangGraph state definition
- [ ] Checkpointing (MemorySaver) for resumable runs
- [ ] `analyze → plan → deploy → verify` graph
- [ ] Human-in-the-loop approval interrupt

## Stage 6 — Native tools ◔

- [ ] Read-only tools: filesystem, process, static analysis
- [x] Tool registry, schema generation, permission tagging (via the MCP client
      layer; in-process tools still need their own path)

## Stage 7 — Deployment planning ◔

- [ ] Typed deployment plan (targets, steps, rollback)
- [ ] Plan validation and diffing
- [ ] Plan rendering in the dashboard

## Stage 8 — Verification ◔

- [ ] Post-deploy health checks
- [ ] Log and metric collection
- [ ] Verification report

## Stage 9 — MCP foundation & GitHub ◔

- [x] MCP client manager (discovery, connection lifecycle, timeouts)
- [x] LangGraph tool graph: discover → build request → policy → invoke
- [x] Deny-by-default execution policy (unknown tools, command execution,
      argument screening, approval gate)
- [x] Safe demo server (`mcp_servers/demo_server.py`) for offline development
- [x] Approval enforcement for mutating tools
- [x] `GET /api/mcp/tools`, `POST /api/mcp/tools/call`
- [x] `mcp_servers/github` — read-only: repo, branches, commits, issues, pull
      requests, file tree, file contents
- [x] GitHub repository analysis workflow with a scored deployment-readiness
      assessment (`POST /api/github/repository/analyze`)

## Stage 10 — MCP: Docker ◔

- [x] `mcp_servers/docker` — availability, build, images, start, stop, status,
      health, logs (no shell, no push, no registry credentials)
- [x] Docker deployment workflow: inspect → build → run → health check → logs
- [x] Non-shell container start (no entrypoint override, no `--health-cmd`)
- [ ] Image build driven by a generated plan

## Stage 10b — Unified deployment planning ✅

- [x] `RepositoryDeploymentPlan`: stack, five strategies, env vars, services, risks,
      approvals, ordered steps
- [x] One graph combining repository analysis, DevOps planning and risk assessment
- [x] Dockerfile → Docker build; no Dockerfile → recommend one, never write it
- [x] Tests present → run as a gate; absent → marked limited, never a pass
- [x] Placeholder env values block the deployment until resolved
- [x] Blocking propagates: no build means no image, so dependent steps are blocked too
- [x] Machine-readable plan plus a Markdown rendering of the same fields
- [ ] Executor that consumes a plan — deliberately separate from planning

## Stage 11 — Real deployment flow ⬜

- [ ] Execute a plan end to end
- [ ] Idempotency and retry with backoff
- [ ] Rollback on failure

## Stage 12 — MCP: Kubernetes ⬜

- [ ] `mcp_servers/kubernetes` — apply, rollout status, pod logs

## Stage 13 — MCP: AWS ⬜

- [ ] `mcp_servers/aws` — deploy, describe, logs, rollback
- [ ] Credential handling and least-privilege roles

## Stage 14 — Failure investigation ⬜

- [ ] Failure classification agent
- [ ] Log correlation across services
- [ ] Root-cause summary in the dashboard

## Stage 15 — Safe self-healing ⬜

- [ ] Remediation proposals generated, never auto-applied at first
- [ ] Blast-radius checks before any destructive action
- [ ] Full audit trail per remediation

## Stage 16 — Dashboard maturity ⬜

- [ ] Deployment timeline and live agent run view
- [ ] Approval UI
- [ ] Failure and remediation history

## Stage 17 — Evaluation ⬜

- [ ] Scenario suite with pass/fail criteria
- [ ] Regression harness across agent prompt changes

## Stage 18 — Hardening ⬜

- [ ] AuthN/AuthZ on the API
- [ ] Rate limiting and abuse guards
- [ ] Observability: metrics, tracing, alerting
- [ ] Production deployment guide

## Explicitly out of scope until the stages above

AWS integration · registry push · write-capable GitHub tools · autonomous
deployment · self-healing. The packages that will hold them exist and are
documented, but contain no implementation.