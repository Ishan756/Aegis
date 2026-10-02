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
                                  │ stdio (planned)
                    ┌─────────────▼─────────────┐
                    │  mcp_servers/             │
                    │  github · docker · k8s    │
                    │  aws                      │
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

Long-running agent runs will be checkpointed so an interrupted deployment can be
resumed and audited. Redis is the intended store for hot state (status, locks);
Postgres for durable history (deployments, approvals, tool call records). The
boundary is declared in `app/cache/` and `app/db/` but not yet wired.

## Safety model (planned)

The agent is autonomous but not unsupervised. Three escalating levels:

1. **Read-only** — analysis and planning. No approval needed.
2. **Mutating** — creating a branch, opening a PR, applying a manifest.
   Requires explicit human approval, recorded with the approver identity.
3. **Destructive** — deleting, rolling back, terminating. Requires approval and
   a verified rollback path.

Levels are assigned per tool, declared in the MCP server, and enforced centrally
in `app/mcp` so an individual agent cannot escalate its own permissions.

## Current status

Only the foundation is implemented: configuration, logging, the HTTP health
surface and the dashboard. Every other package is an empty, documented boundary.
See `docs/roadmap.md` for what gets built next.