import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import App from './App'
import type { HealthResponse } from './types/health'

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