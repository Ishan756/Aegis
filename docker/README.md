# Docker Assets

Shared Docker configuration for local development. The Dockerfiles themselves
live next to the code they build (`backend/Dockerfile`, `frontend/Dockerfile`)
so each service stays self-contained.

| Path                     | Purpose                                                     |
| ------------------------ | ----------------------------------------------------------- |
| `nginx/default.conf`     | Serves the built SPA and proxies `/api` to the backend      |
| `postgres/init/`         | Init SQL mounted into the optional Postgres service          |

## Running

```bash
docker compose up --build
```

- Dashboard: http://localhost:8080
- API + Swagger: http://localhost:8000/docs

## Optional infrastructure

Postgres and Redis are declared behind the `infra` profile, so they **do not
start** with a plain `docker compose up` and are not required by any code yet:

```bash
docker compose --profile infra up --build
```

Set `AEGIS_DATABASE_URL` / `AEGIS_REDIS_URL` (see `.env.example`) if you enable
them; until the relevant stage lands, setting them only changes what the health
endpoint reports, it does not open a connection.

## Caveat

These files were authored but not executed in the environment where the
foundation was built (Docker was unavailable). Treat `docker compose up` as
unverified until you run it locally.