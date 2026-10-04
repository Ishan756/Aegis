import type { DeploymentDetail, DeploymentListResponse, DeploymentStatus } from '../types/deployment'
import type { HealthResponse } from '../types/health'

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '')
const TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS ?? 5000)

/**
 * Root for the unversioned routes.
 *
 * Health is served at `/api/v1/health` while deployment history is at
 * `/api/deployments`. Deriving the root from the configured base means one
 * variable configures both, and a proxy that mounts the API somewhere else only
 * has to be described once.
 */
const ROOT_URL = (
  import.meta.env.VITE_API_ROOT_URL ?? BASE_URL.replace(/\/v\d+$/, '')
).replace(/\/$/, '')

/** Error thrown when the backend cannot be reached or returns a failure. */
export class ApiError extends Error {
  constructor(
    message: string,
    override readonly cause?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * Fetch the backend health payload.
 *
 * Requests are bounded by a timeout so a hung backend surfaces as an offline
 * dashboard rather than a spinner that never resolves.
 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)

  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  try {
    const response = await fetch(`${BASE_URL}/health`, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })

    if (!response.ok) {
      throw new ApiError(`Backend responded with HTTP ${response.status}`)
    }

    return (await response.json()) as HealthResponse
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(
        signal?.aborted ? 'Request cancelled' : `Backend did not respond within ${TIMEOUT_MS}ms`,
        error,
      )
    }
    throw new ApiError('Could not reach the Aegis backend', error)
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

/** Filters the history list accepts. Omitted fields mean "no filter". */
export interface DeploymentFilters {
  repository?: string
  status?: DeploymentStatus
  limit?: number
  offset?: number
}

/**
 * Request JSON, sharing one timeout and error vocabulary with the health probe.
 *
 * Without this the two calls would fail differently — one reporting HTTP 500 as
 * a failure, the other as "could not reach the backend" — and the dashboard
 * could not tell a broken deployment from a broken connection.
 *
 * `subject` names what was being loaded so two simultaneous failures do not
 * render as the same message twice on one screen.
 */
async function getJson<T>(url: string, subject: string, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)

  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  try {
    const response = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    })

    if (!response.ok) {
      throw new ApiError(`Could not load ${subject} (HTTP ${response.status})`)
    }

    return (await response.json()) as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (controller.signal.aborted) {
      throw new ApiError(
        signal?.aborted ? 'Request cancelled' : `Backend did not respond within ${TIMEOUT_MS}ms`,
        error,
      )
    }
    // Phrased differently from the health probe on purpose: health reports a
    // connection failure as one, and two identical messages on a dashboard tell
    // an operator nothing about which call failed.
    throw new ApiError(`Could not load ${subject}`, error)
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

/** Fetch one page of deployment history, newest first. */
export function fetchDeployments(
  filters: DeploymentFilters = {},
  signal?: AbortSignal,
): Promise<DeploymentListResponse> {
  const params = new URLSearchParams()
  if (filters.repository) params.set('repository', filters.repository)
  if (filters.status) params.set('status', filters.status)
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  if (filters.offset !== undefined) params.set('offset', String(filters.offset))

  const query = params.toString()
  return getJson<DeploymentListResponse>(
    `${ROOT_URL}/deployments${query ? `?${query}` : ''}`,
    'deployment history',
    signal,
  )
}

/** Fetch one deployment's full execution trace, including its lessons. */
export function fetchDeployment(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<DeploymentDetail> {
  return getJson<DeploymentDetail>(
    `${ROOT_URL}/deployments/${encodeURIComponent(deploymentId)}`,
    'that deployment',
    signal,
  )
}

/** Render one deployment's trace as markdown, for copying into a ticket. */
export async function fetchDeploymentMarkdown(
  deploymentId: string,
  signal?: AbortSignal,
): Promise<string> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  try {
    const response = await fetch(
      `${ROOT_URL}/deployments/${encodeURIComponent(deploymentId)}?format=markdown`,
      { headers: { Accept: 'text/plain' }, signal: controller.signal },
    )
    if (!response.ok) {
      throw new ApiError(`Could not load the trace (HTTP ${response.status})`)
    }
    return await response.text()
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}