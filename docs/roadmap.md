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

- [x] Post-deploy health checks: seven ordered checks from container existence to
      dependency reachability
- [x] Published ports are probed, not merely read — a mapped port with nothing
      listening passes every status check and fails every request
- [x] Log scanning with separate fatal and benign pattern sets
- [x] `SUCCESS` / `WARNING` / `FAILED`, where `WARNING` is not a pass
- [x] Machine-readable result plus a Markdown report
- [ ] Metric collection

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

## Stage 10c — Sequential execution engine ✅

- [x] Ordered task execution; order encodes causality, not preference
- [x] Per-attempt audit trail with status, duration, error code and error class
- [x] Error classification (`TRANSIENT` / `PERMANENT` / `POLICY` / `TIMEOUT`);
      unrecognised errors default to non-retryable
- [x] Per-task timeouts via `asyncio.wait_for`
- [x] Policy enforced on every tool call, including manual retries
- [x] Only a critical failure stops the run — `stopped` and `failed` are
      different events and are not conflated
- [x] Secret-like arguments redacted before recording; a URL credential keeps its
      scheme and host so the trail stays useful
- [ ] Consume `RepositoryDeploymentPlan` directly rather than a task list

## Stage 10d — Deployment workflow & Debug placeholder ✅

- [x] `PLAN → EXECUTE → VERIFY → END` with `DEBUG` on both failure paths
- [x] Only a clean `SUCCESS` ends the run; `WARNING` routes to `DEBUG`
- [x] Dry run ends without verifying, since nothing was deployed
- [x] Debug Agent classifies failures, cites evidence, proposes fixes with
      `requires_approval` — and applies nothing
- [x] `POST /api/deployment/execute`, `/api/deployment/workflow`,
      `/api/verification/deployment`
- [x] Read-only loopback `http_probe` tool with no host, method, header or body
      parameter, and no redirect following
- [ ] Remediation proposals executed by a human approval flow (Stage 15)

## Stage 11 — Real deployment flow ◔

- [x] Execute a task list end to end
- [x] Retry with backoff
- [ ] Consume a `RepositoryDeploymentPlan` end to end
- [ ] Idempotency keys, so a retried deployment cannot double-start a container
- [ ] Rollback on failure — proposed only, never automatic

## Stage 12 — MCP: Kubernetes ⬜

- [ ] `mcp_servers/kubernetes` — apply, rollout status, pod logs

## Stage 13 — MCP: AWS ⬜

- [ ] `mcp_servers/aws` — deploy, describe, logs, rollback
- [ ] Credential handling and least-privilege roles

## Stage 14 — Failure investigation ✅

- [x] Failure classification agent (`app/agents/investigation.py`)
- [x] Evidence-backed root causes — a cause with no citing observation is rejected
- [x] Read-only evidence: container status/health/logs, HTTP probe, recent commits,
      Dockerfile and repository configuration
- [x] Confidence derived from independent corroboration, not asserted
- [x] `POST /api/deployment/incident`, JSON or Markdown
- [ ] Log correlation across services
- [ ] Root-cause summary in the dashboard

## Stage 15 — Safe self-healing ✅

- [x] Remediation proposals generated and never auto-applied; `applied` is always
      `false` and every proposal carries `requires_approval`
- [x] Human approval flow that can actually execute a proposal
      (`human_approved` is a request field, never set by the graph)
- [x] Blast-radius checks before any destructive action
- [x] Full audit trail per remediation — applied *and* declined attempts
- [x] Bounded loop: `max_recovery_attempts` (capped at 10), plus an independent
      step limit so a routing bug cannot make it unbounded
- [x] Non-destructive automatic actions only: retry, transient retry, restart
- [x] Code and configuration changes are `FORBIDDEN` under every setting — there
      is no flag that enables them
- [x] Image rebuild is `APPROVAL_REQUIRED` and off by default, because it
      re-executes untrusted Dockerfile `RUN` steps
- [x] Disabled by default; a fresh install diagnoses and escalates
- [ ] Restart reimplemented as a single non-destructive tool, so it does not
      route through the high-risk `docker.stop_container`
- [ ] Kubernetes rollout equivalents for each fix category

## Stage 15a — Deployment history ✅

- [x] `DeploymentRecord`: repository, commit, plan, tasks, actions, verification,
      per-stage failures, recovery attempts, incident, status and timestamps
- [x] `MemoryStore` protocol with in-memory and PostgreSQL implementations, so
      the API never learns which one it is talking to
- [x] PostgreSQL JSONB for the nested trace, indexed columns for what is
      filtered and sorted, upsert on `deployment_id`
- [x] Lessons keyed on a stable fingerprint: the same cause in the same
      repository increments one row instead of accumulating duplicates
- [x] `GET /api/deployments` and `GET /api/deployments/{id}`, plus
      `?format=markdown` for pasting into a ticket
- [x] History survives a database outage: persistence is best-effort and a
      failed write is logged, not raised
- [x] Dashboard history list and execution-trace view
- [ ] Lessons recalled automatically before a deployment runs
- [ ] A migration runner, once the schema starts changing in the field

## Stage 16 — Dashboard maturity ⬜

- [ ] Live agent run view (history is recorded, not streamed)
- [ ] Approval UI
- [ ] Historical trend charts over the stored records

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
deployment. Self-healing exists but is bounded, off by default, and cannot edit
code or configuration; the Kubernetes equivalents of these fix categories are not
built.