import type { HealthResponse } from '../types/health'

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '')
const TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS ?? 5000)

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