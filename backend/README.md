# Aegis Backend

FastAPI service that hosts Aegis' HTTP API and (from later stages) the LangGraph
agent runtime.

## Layout

| Path              | Responsibility                                          |
| ----------------- | ------------------------------------------------------- |
| `app/main.py`     | Application factory, middleware, lifespan               |
| `app/api/`        | Routers and FastAPI dependencies                         |
| `app/core/`       | Settings, logging, cross-cutting helpers                |
| `app/models/`     | Pydantic data contracts                                 |
| `app/services/`   | Business logic                                          |
| `app/agents/`     | LLM-backed actors (planned)                              |
| `app/graph/`      | LangGraph state graphs (planned)                         |
| `app/tools/`      | Native agent tools (planned)                             |
| `app/mcp/`        | MCP client layer (planned)                              |
| `app/db/`         | SQLAlchemy engine + migrations (planned)                 |
| `app/cache/`      | Redis wrapper for ephemeral state (planned)              |
| `tests/`          | Unit and API tests                                      |

## Local development

Requires [uv](https://docs.astral.sh/uv/) (or any Python 3.11+ environment).

```bash
uv venv .venv
uv pip install -e ".[dev]"
uv run --no-project uvicorn app.main:app --reload --port 8000
```

Or without uv:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
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

- LangGraph and the MCP SDK are declared in the optional `agent` extra and are
  not installed by default; install with `-e ".[dev,agent]"` when the agent
  runtime lands.
- PostgreSQL and Redis are not required. If `AEGIS_DATABASE_URL` is set the
  health endpoint reports the database as configured, but no connection is
  opened yet.