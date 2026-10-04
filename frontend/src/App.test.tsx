import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import App from './App'
import type { HealthResponse } from './types/health'
import type { DeploymentDetail, DeploymentListResponse } from './types/deployment'

const HEALTHY: HealthResponse = {
  status: 'ok',
  service: 'Aegis Backend',
  version: '0.1.0',
  environment: 'local',
  uptime_seconds: 90,
  stage: 'stage-1-foundation',
  components: [
    { name: 'api', status: 'ok', detail: 'Serving requests' },
    { name: 'mcp', status: 'not_configured', detail: 'No MCP servers registered' },
  ],
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('renders the Aegis identity', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(HEALTHY))))

    render(<App />)

    expect(screen.getByRole('heading', { name: 'Aegis' })).toBeTruthy()
    expect(screen.getByText('Autonomous AI DevOps Engineer')).toBeTruthy()
    expect(await screen.findByText('System Status')).toBeTruthy()
    expect(screen.getByText('Backend Connection')).toBeTruthy()
  })

  it('surfaces backend telemetry once reachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(HEALTHY))))

    render(<App />)

    expect(await screen.findByText('Connected')).toBeTruthy()
    // Rendered both in the masthead badge and in the System Status metrics.
    expect(screen.getAllByText('stage-1-foundation').length).toBe(2)
    expect(screen.getByText('1/2 operational')).toBeTruthy()
  })

  it('reports an offline backend instead of crashing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))

    render(<App />)

    expect(await screen.findByText('Disconnected')).toBeTruthy()
    expect(screen.getByText(/Could not reach the Aegis backend/)).toBeTruthy()
  })
})

/** Route a URL to a canned response so the dashboard's two calls coexist. */
function stubBackend(routes: {
  health?: unknown
  history?: DeploymentListResponse
  detail?: DeploymentDetail
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)

    if (url.includes('/health')) {
      return new Response(JSON.stringify(routes.health ?? HEALTHY))
    }
    if (url.includes('/deployments/')) {
      return routes.detail
        ? new Response(JSON.stringify(routes.detail))
        : new Response(JSON.stringify({ error: { code: 'not_found' } }), { status: 404 })
    }
    if (url.includes('/deployments')) {
      return new Response(JSON.stringify(routes.history ?? { items: [], total: 0, limit: 20, offset: 0 }))
    }
    return new Response('{}', { status: 404 })
  })

  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const HISTORY: DeploymentListResponse = {
  items: [
    {
      deployment_id: 'dep-1',
      repository: 'acme/api',
      repository_path: '../examples/sample_app',
      commit_sha: 'abcdef1234567890',
      commit_ref: 'main',
      image: 'acme/api:dev',
      container: 'acme-api',
      status: 'succeeded',
      recovered: false,
      escalated: false,
      escalation_reason: null,
      dry_run: false,
      action_count: 3,
      failure_count: 0,
      started_at: '2026-01-01T12:00:00Z',
      finished_at: '2026-01-01T12:00:12Z',
      duration_seconds: 12,
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
}

const DETAIL: DeploymentDetail = {
  ...HISTORY.items[0],
  run_id: 'run-1',
  error: null,
  plan: null,
  tasks: [],
  actions: [],
  verification: null,
  failures: [],
  recovery_attempts: [],
  incident: null,
  request: null,
  created_at: '2026-01-01T12:00:00Z',
  updated_at: '2026-01-01T12:00:12Z',
  lessons: [],
}

describe('App deployment history', () => {
  it('shows history alongside health', async () => {
    stubBackend({ history: HISTORY })

    render(<App />)

    expect(await screen.findByText('Deployment History')).toBeTruthy()
    expect(await screen.findByText('1 recorded')).toBeTruthy()
    expect(await screen.findByText('acme/api:dev')).toBeTruthy()
  })

  it('reports an empty history without treating it as an error', async () => {
    stubBackend({ history: { items: [], total: 0, limit: 20, offset: 0 } })

    render(<App />)

    expect(await screen.findByText(/No deployments recorded yet/)).toBeTruthy()
  })

  it('opens a trace when a history row is selected', async () => {
    stubBackend({ history: HISTORY, detail: DETAIL })

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /Open trace for dep-1/ }))

    expect(await screen.findByRole('heading', { name: 'Deployment dep-1' })).toBeTruthy()
    // Present in the history row and again in the trace's own facts.
    expect(screen.getAllByText('acme/api:dev').length).toBeGreaterThan(0)
  })

  it('closes a trace again', async () => {
    stubBackend({ history: HISTORY, detail: DETAIL })

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /Open trace for dep-1/ }))
    await screen.findByRole('heading', { name: 'Deployment dep-1' })

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Deployment dep-1' })).toBeNull())
  })

  it('reports a history failure without hiding the rest of the dashboard', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/health')) return new Response(JSON.stringify(HEALTHY))
      return new Response('{}', { status: 500 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(await screen.findByText(/Could not load deployment history/)).toBeTruthy()
    // The health panel still renders: one broken call must not blank the page.
    expect(screen.getByText('Connected')).toBeTruthy()
  })

  it('requests history from the unversioned route', async () => {
    const fetchMock = stubBackend({ history: HISTORY })

    render(<App />)

    await screen.findByText('1 recorded')

    const urls = fetchMock.mock.calls.map((call) => String(call[0]))
    expect(urls.some((url) => url.startsWith('/api/v1/health'))).toBe(true)
    expect(urls.some((url) => url.startsWith('/api/deployments'))).toBe(true)
  })
})
