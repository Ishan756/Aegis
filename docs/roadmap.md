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

## Stage 2 — Configuration & secrets ◔

- [ ] Secret management boundary (no secrets in code or logs)
- [ ] Typed client settings with validation
- [ ] Request correlation IDs and structured log fields

## Stage 3 — Persistence ◔

- [ ] SQLAlchemy async engine, session factory, migrations (Alembic)
- [ ] Core tables: deployments, tool_calls, approvals, agent_runs
- [ ] Redis wrapper for hot state and locks

## Stage 4 — Agent foundation ◔

- [ ] LLM provider abstraction with a single testable fake
- [ ] Structured output schemas for every agent response
- [ ] Repository-analysis agent (language, build system, entrypoints, Dockerfile)

## Stage 5 — Orchestration ◔

- [ ] LangGraph state definition and checkpointing
- [ ] `analyze → plan → deploy → verify` graph
- [ ] Human-in-the-loop approval interrupt

## Stage 6 — Native tools ◔

- [ ] Read-only tools: filesystem, process, static analysis
- [ ] Tool registry, schema generation, permission tagging

## Stage 7 — Deployment planning ◔

- [ ] Typed deployment plan (targets, steps, rollback)
- [ ] Plan validation and diffing
- [ ] Plan rendering in the dashboard

## Stage 8 — Verification ◔

- [ ] Post-deploy health checks
- [ ] Log and metric collection
- [ ] Verification report

## Stage 9 — MCP foundation & GitHub ⬜

- [ ] MCP client manager (discovery, connection lifecycle, timeouts)
- [ ] `mcp_servers/github` — read repo, list branches, read file
- [ ] Approval enforcement for mutating tools

## Stage 10 — MCP: Docker ⬜

- [ ] `mcp_servers/docker` — build, push, inspect, logs
- [ ] Image build from a generated plan

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

AWS integration · GitHub credentials · real MCP servers · autonomous
deployment · self-healing. The packages that will hold them exist and are
documented, but contain no implementation.