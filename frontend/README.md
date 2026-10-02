# Aegis Frontend

React + Vite + TypeScript dashboard. In stage 1 it renders the Aegis identity
and polls the backend health endpoint.

## Getting started

```bash
npm install
npm run dev        # http://localhost:5173
```

The dev server proxies `/api` to `BACKEND_URL` (default
`http://localhost:8000`), so the browser stays on one origin and there is no
CORS configuration to maintain during development. The containerized version
proxies the same path via nginx — see `docker/nginx/default.conf`.

Override for a backend running in Docker:

```bash
BACKEND_URL=http://localhost:8000 npm run dev
```

## Structure

```
src/
  main.tsx                 entrypoint
  App.tsx                  layout and data wiring
  index.css                global styles and design tokens
  components/
    StatusCard.tsx         card shell with status badge
    SystemStatus.tsx       aggregate health from the backend
    BackendConnection.tsx  reachability, latency, last contact
    ComponentList.tsx      per-subsystem breakdown
    tone.ts                status -> visual tone mapping
  hooks/
    useHealth.ts           polling health probe with cleanup
  lib/
    api.ts                 typed fetch client with timeout
  types/
    health.ts              mirrors backend/app/models/health.py
```

`types/health.ts` is a deliberate duplicate of the backend schema: it is the
wire contract, and keeping it explicit means a backend change that breaks the
dashboard fails at compile time instead of rendering blank fields.

## Commands

| Command              | Purpose                                  |
| -------------------- | ---------------------------------------- |
| `npm run dev`        | Dev server with HMR                      |
| `npm run build`      | Typecheck, then production bundle        |
| `npm run preview`    | Serve the built bundle on :4173          |
| `npm test`           | Vitest component tests                   |
| `npm run lint`       | ESLint                                   |
| `npm run typecheck`  | `tsc -b --noEmit`                        |

Tests use `happy-dom` rather than `jsdom` because it starts much faster, which
matters when `node_modules` sits on a slow mounted filesystem.

## Environment

| Variable               | Default   | Purpose                            |
| ---------------------- | --------- | ---------------------------------- |
| `VITE_API_BASE_URL`    | `/api/v1` | Base URL for API calls             |
| `VITE_API_TIMEOUT_MS`  | `5000`    | Per-request timeout                |
| `BACKEND_URL`          | `http://localhost:8000` | Dev-server proxy target (not shipped to the browser) |

Vite inlines `VITE_*` variables at build time, so they cannot be changed by
setting them in the browser.