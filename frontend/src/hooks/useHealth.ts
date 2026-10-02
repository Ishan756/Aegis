import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchHealth } from '../lib/api'
import type { HealthResponse } from '../types/health'

/** Polling interval for the health probe, in milliseconds. */
const POLL_INTERVAL_MS = 10_000

export type ConnectionState = 'connecting' | 'online' | 'offline'

export interface HealthState {
  connection: ConnectionState
  health: HealthResponse | null
  error: string | null
  /** Round-trip time of the most recent successful probe, in milliseconds. */
  latencyMs: number | null
  lastChecked: Date | null
  /** Trigger an immediate probe, e.g. from a "Retry" button. */
  refresh: () => void
}

/**
 * Poll the backend health endpoint on an interval.
 *
 * Kept as a hook so the dashboard and any future agent-activity views share a
 * single connection state.
 */
export function useHealth(intervalMs: number = POLL_INTERVAL_MS): HealthState {
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)
  const [nonce, setNonce] = useState(0)

  // Kept in a ref so the polling effect does not restart when the interval
  // or refresh counter change.
  const activeRequest = useRef<AbortController | null>(null)

  const refresh = useCallback(() => setNonce((value) => value + 1), [])

  useEffect(() => {
    let cancelled = false

    const probe = async () => {
      // Cancel any in-flight probe so slow responses cannot overwrite a newer one.
      activeRequest.current?.abort()
      const controller = new AbortController()
      activeRequest.current = controller

      const startedAt = performance.now()
      try {
        const payload = await fetchHealth(controller.signal)
        if (cancelled) return
        setHealth(payload)
        setLatencyMs(Math.round(performance.now() - startedAt))
        setConnection('online')
        setError(null)
      } catch (cause) {
        if (cancelled || controller.signal.aborted) return
        setHealth(null)
        setLatencyMs(null)
        setConnection('offline')
        setError(cause instanceof Error ? cause.message : 'Unknown error')
      } finally {
        if (!cancelled) setLastChecked(new Date())
      }
    }

    void probe()
    const timer = setInterval(probe, intervalMs)

    return () => {
      cancelled = true
      clearInterval(timer)
      activeRequest.current?.abort()
    }
  }, [intervalMs, nonce])

  return { connection, health, error, latencyMs, lastChecked, refresh }
}