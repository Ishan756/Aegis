# MCP Servers

Aegis will reach GitHub, Docker and cloud providers through
[Model Context Protocol](https://modelcontextprotocol.io) servers. Each server
is a standalone process exposing a narrow set of audited tools; the backend's
`app/mcp` client layer discovers and invokes them.

**No servers exist yet.** This directory holds the contract they will follow,
so the boundary is decided before any credentials or infrastructure access are
introduced.

## Planned servers

| Server              | Tools it will expose                                       | Stage   |
| ------------------- | ---------------------------------------------------------- | ------- |
| `github/`           | read repo, list branches, read file, open PR, read checks   | Stage 9 |
| `docker/`           | build image, push image, inspect container, read logs       | Stage 10 |
| `kubernetes/`       | apply manifest, rollout status, pod logs                    | Stage 12 |
| `aws/`              | describe instances, deploy, tail logs, rollback             | Stage 13 |

## Rules each server must follow

1. **Read and write are separate tools.** e.g. `github.read_file` vs
   `github.create_pull_request`. The agent should never mutate state with a
   tool whose name does not say so.
2. **Destructive tools declare their blast radius.** Anything that deletes,
   rolls back or terminates gets an explicit `destructive: true` marker and
   requires human approval (see the approval gate in `docs/roadmap.md`).
3. **No ambient credentials.** Servers read secrets from the environment only;
   no tokens are ever written to disk or returned in a response.
4. **Structured errors.** Return a typed error, never a stack trace, so the
   agent can decide whether to retry or escalate.
5. **Local stdio transport by default.** Servers run as child processes of the
   backend, which keeps permissions scoped to the Aegis process.

## Layout (once implemented)

```
mcp_servers/
  github/
    server.py        # FastMCP app, tool definitions
    README.md
  docker/
    server.py
    README.md
```

A shared helper module for common concerns (error types, approval metadata,
logging) will live in `mcp_servers/common/` once a second server exists —
extracting it earlier would be guesswork.