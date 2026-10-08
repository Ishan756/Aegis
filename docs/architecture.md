# Aegis Architecture

Aegis is an AI agent that analyses repositories, builds deployment plans, and
carries deployments out through audited tools. This document describes the shape
of the system as it stands after the foundation stage, and why the boundaries
were drawn where they were.

## Component overview

```
                    ┌───────────────────────────┐
   Browser ────────▶│  frontend/  React + Vite  │
                    │  operator dashboard       │
                    └─────────────┬─────────────┘
                                  │ HTTP (JSON)
                    ┌─────────────▼─────────────┐
                    │  backend/   FastAPI       │
                    │  ┌──────────────────────┐ │
                    │  │ api/     HTTP layer   │ │
                    │  │ services/  logic     │ │
                    │  │ agents/   LLM actors  │ │
                    │  │ graph/    LangGraph   │ │
                    │  │ tools/    native ops  │ │
                    │  │ mcp/      MCP client  │ │
                    │  └──────────┬───────────┘ │
                    └─────────────┼─────────────┘
                                  │ stdio (MCP)
                    ┌─────────────▼─────────────┐
                    │  mcp_servers/             │
                    │  github · docker · k8s    │
                    │  aws                      │
                    └─────────────┬─────────────┘
                                  │ DOCKER_HOST=ssh://user@host
                                  │ (scoped server, per run)
                    ┌─────────────▼─────────────┐
                    │  EC2 instance             │
                    │  Docker daemon + app      │
                    └───────────────────────────┘
```

## Why these boundaries

**The agent never talks to infrastructure directly.** Every mutation goes
through an MCP server. This gives one place to enforce approval policy, one
place to log an audit trail, and the option to revoke a capability without
touching agent code.

**HTTP is not the agent's interface.** The graph invokes Python callables and
MCP tools in-process. The HTTP API exists for the dashboard and for humans, so
an agent bug can never become an unauthenticated network call.

**Writes are separated from reads at the tool level.** `github.read_file` and
`github.create_pull_request` are different tools. A tool that mutates state must
say so in its name, and must be marked `destructive` when it deletes or rolls
back. This is what makes an approval gate possible later.

**Storage is optional and replaceable.** The health endpoint reports what is
configured rather than pretending. Postgres and Redis are declared so the
dependency edges are visible, but nothing requires them, so the project boots
with no infrastructure at all.

**Remote targets change where a call goes, never what is allowed.** A
deployment to EC2 opens a *scoped* Docker MCP server whose environment sets
`DOCKER_HOST=ssh://user@host`; tools registered under that server's name are
evaluated by the same policy as the local ones. The local server is never
repointed, request bodies never carry a target (it is configuration), and SSH
commands — which do not pass through the MCP policy — take the request's
`approve` flag as their gate. See `docs/ec2-deployment.md`.

## Backend layering

Dependencies point in one direction only:

```
api  ──▶  services  ──▶  agents / graph / tools  ──▶  mcp
 │             │
 └──▶ models ──┘
core   (config, logging — imported by everything, imports nothing)
```

A route handler should parse a request, call a service, and return a schema.
Anything with real behaviour belongs in `services/`. `core/` never imports from
the layers above it, so configuration and logging cannot create cycles.

## State and checkpoints

Deployment history is written through the `MemoryStore` protocol with an
in-memory implementation (default) and a PostgreSQL one selected by
configuration, so the API never learns which it is talking to; persistence is
best-effort and a failed write is logged, not raised. Every run is recorded as
in-progress before its first stage, so an interrupted deployment reads as
interrupted rather than absent. Redis is still the intended store for hot state
(status, locks); that boundary is declared but not wired.

## Safety model

The agent is autonomous but not unsupervised. Three escalating levels:

1. **Read-only** — analysis, planning, status probes. No approval needed.
2. **Mutating** — build, start, branch creation. Requires explicit human
   approval, recorded with the request's approval reference.
3. **Destructive** — stop, rollback. Requires approval and is separately marked
   in the policy.

Levels are assigned per tool, declared in the MCP server, and enforced centrally
by `app/services/mcp_policy.py`: the policy is deny-by-default, refuses
dangerous argument shapes outright, and distinguishes "may run" from "may run
once a human has approved". An individual agent cannot escalate its own
permissions, because it never decides — it submits a `ToolCallRequest` and the
policy answers.

Commands that bypass MCP (the SSH preparation for a remote target) take the
request's `approve` flag as their gate, checked in the same code that would
launch them. Self-healing is off by default and cannot edit code or
configuration under any setting.

## Current status

Configuration, logging, the HTTP surface, repository analysis, deployment
planning, the sequential execution engine, the PLAN → EXECUTE → VERIFY workflow,
seven-check verification, evidence-backed failure investigation, bounded
self-healing (off by default), persistent deployment history, and the GitHub,
Docker and AWS MCP servers are implemented. Stage 16 adds deployments to a
configured EC2 instance over SSH and a scoped Docker server.
`docs/roadmap.md` tracks what remains.