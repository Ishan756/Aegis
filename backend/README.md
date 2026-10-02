# Aegis Backend

FastAPI service that hosts Aegis' HTTP API and the LangGraph agent runtime.

## Layout

| Path                  | Responsibility                                            |
| --------------------- | --------------------------------------------------------- |
| `app/main.py`         | Application factory, middleware, lifespan                 |
| `app/api/`            | Routers, error handlers, FastAPI dependencies             |
| `app/api/routes/`     | One module per API area (`health`, `agent`)               |
| `app/core/`           | Settings, logging, request middleware, exception types    |
| `app/models/`         | Pydantic contracts (`health`, `errors`, `planning`)       |
| `app/services/`       | Business logic, including the LLM provider interface      |
| `app/agents/`         | LangGraph state graphs                                    |
| `tests/`              | Unit and API tests                                        |

## Endpoints

| Method | Path               | Purpose                          |
| ------ | ------------------ | -------------------------------- |
| `GET`  | `/`                | Service banner                   |
| `GET`  | `/api/v1/health`   | Aggregate health + components    |
| `GET`  | `/api/v1/health/live` | Liveness probe                |
| `POST` | `/api/agent/plan`  | Generate a DevOps execution plan |

Health routes are versioned under `/api/v1`; the agent route is intentionally
unversioned because the response shape is still pre-1.0.

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

- LangGraph is required by the planning agent and lives in the optional `agent`
  extra. Installing `-e ".[dev]"` alone will fail to import the graph, so use
  `-e ".[agent,dev]"`.
- The MCP SDK is **not** installed yet. It enters the `agent` extra when the MCP
  stage begins.
- PostgreSQL and Redis are not required. If `AEGIS_DATABASE_URL` is set the
  health endpoint reports the database as configured, but no connection is
  opened yet.