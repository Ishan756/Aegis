# Cross-Service Tests

Tests that span more than one component live here. Unit and API tests stay
next to the code they cover (`backend/tests/` and `frontend/src/**/*.test.tsx`).

## Contents

- `smoke/` — checks a *running* stack rather than imported code.

## Running

Start the backend first, then:

```bash
make smoke
```

Or directly:

```bash
backend/.venv/bin/python -m pytest tests -q
```

The smoke tests **skip** when the backend is not reachable, so `make test` stays
safe to run without Docker or a dev server. Point them at another host with
`AEGIS_SMOKE_URL`:

```bash
AEGIS_SMOKE_URL=http://localhost:8000/api/v1 backend/.venv/bin/python -m pytest tests -q
```