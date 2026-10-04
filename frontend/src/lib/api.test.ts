import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, fetchDeployment, fetchDeploymentMarkdown, fetchDeployments, fetchHealth } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Stub fetch and hand back the URL it was called with. */
function stubFetch(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(typeof body === 'string' ? body : JSON.stringify(body), {
      status: init.status ?? 200,
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('api prefixes', () => {
  it('reads health from the versioned base', async () => {
    const fetchMock = stubFetch({ status: 'ok' })

    await fetchHealth()

    // Health is the versioned route: /api/v1/health.
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/health')
  })

  it('reads deployment history from the unversioned root', async () => {
    const fetchMock = stubFetch({ items: [], total: 0, limit: 20, offset: 0 })

    await fetchDeployments()

    // History is not versioned, so it must not inherit the /v1 segment. Asking
    // for /api/v1/deployments returns a 404 that looks like a backend outage.
    expect(fetchMock.mock.calls[0][0]).toBe('/api/deployments')
  })

  it('reads a single trace from the unversioned root', async () => {
    const fetchMock = stubFetch({ deployment_id: 'dep-1' })

    await fetchDeployment('dep-1')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/deployments/dep-1')
  })
})

describe('fetchDeployments', () => {
  it('sends no query when nothing is filtered', async () => {
    const fetchMock = stubFetch({ items: [], total: 0, limit: 20, offset: 0 })

    await fetchDeployments()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/deployments')
  })

  it('sends each filter it is given', async () => {
    const fetchMock = stubFetch({ items: [], total: 0, limit: 20, offset: 0 })

    await fetchDeployments({ repository: 'acme/api', status: 'failed', limit: 10, offset: 20 })

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/deployments?repository=acme%2Fapi&status=failed&limit=10&offset=20',
    )
  })

  it('encodes a repository containing a slash', async () => {
    const fetchMock = stubFetch({ items: [], total: 0, limit: 20, offset: 0 })

    await fetchDeployments({ repository: 'acme/api' })

    // Unencoded, the slash would be read as a path separator and the request
    // would look like a deployment id rather than a filter.
    expect(fetchMock.mock.calls[0][0]).toContain('repository=acme%2Fapi')
  })

  it('returns the parsed page', async () => {
    stubFetch({ items: [{ deployment_id: 'dep-1' }], total: 1, limit: 20, offset: 0 })

    const page = await fetchDeployments()

    expect(page.total).toBe(1)
    expect(page.items[0].deployment_id).toBe('dep-1')
  })
})

describe('fetchDeployment', () => {
  it('escapes an id that contains a slash', async () => {
    const fetchMock = stubFetch({ deployment_id: 'a/b' })

    await fetchDeployment('a/b')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/deployments/a%2Fb')
  })

  it('reports an HTTP failure with the status', async () => {
    stubFetch({}, { status: 404 })

    await expect(fetchDeployment('nope')).rejects.toThrow(/that deployment.*404/)
  })

  it('reports an unreachable backend by what it was loading', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network down')))

    // Naming the subject keeps this message distinct from the health probe's,
    // so an operator can tell two simultaneous failures apart.
    await expect(fetchDeployment('dep-1')).rejects.toThrow('Could not load that deployment')
  })
})

describe('fetchDeploymentMarkdown', () => {
  it('asks for the markdown rendering', async () => {
    const fetchMock = stubFetch('# Deployment dep-1')

    const markdown = await fetchDeploymentMarkdown('dep-1')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/deployments/dep-1?format=markdown')
    expect(markdown).toContain('# Deployment dep-1')
  })

  it('does not try to parse the response as JSON', async () => {
    // A plain-text response is not valid JSON; parsing it would throw and report
    // a bug that does not exist.
    stubFetch('# Deployment dep-1')

    await expect(fetchDeploymentMarkdown('dep-1')).resolves.toContain('Deployment')
  })
})

describe('ApiError', () => {
  it('keeps the underlying cause for debugging', async () => {
    const cause = new TypeError('network down')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(cause))

    const error = await fetchHealth().catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).cause).toBe(cause)
  })
})