import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchDeployment, fetchDeployments } from '../lib/api'
import type { DeploymentDetail, DeploymentStatus, DeploymentSummary } from '../types/deployment'

/** Page size. Matches the backend's own default so a first load needs no params. */
export const PAGE_SIZE = 20

export type LoadState = 'loading' | 'ready' | 'error'

export interface DeploymentHistoryState {
  deployments: DeploymentSummary[]
  total: number
  offset: number
  status: LoadState
  error: string | null
  /** The deployment whose trace is open, or null when the list is showing. */
  selected: DeploymentDetail | null
  traceState: LoadState
  traceError: string | null
  statusFilter: DeploymentStatus | null
  setStatusFilter: (status: DeploymentStatus | null) => void
  select: (deploymentId: string | null) => void
  page: (offset: number) => void
  retry: () => void
}

/**
 * Load deployment history and the trace for whichever run is selected.
 *
 * Requests are aborted on unmount and whenever a newer request supersedes them,
 * so a slow page two cannot overwrite page one. The list is fetched once per
 * filter change rather than polled: a deployment history changes when somebody
 * deploys, and polling a ledger nobody is editing only burns requests.
 */
export function useDeploymentHistory(): DeploymentHistoryState {
  const [deployments, setDeployments] = useState<DeploymentSummary[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState<LoadState>('loading')
  const [error, setError] = useState<string | null>(null)

  const [selected, setSelected] = useState<DeploymentDetail | null>(null)
  const [traceState, setTraceState] = useState<LoadState>('loading')
  const [traceError, setTraceError] = useState<string | null>(null)

  const [statusFilter, setStatusFilterState] = useState<DeploymentStatus | null>(null)
  const [nonce, setNonce] = useState(0)

  const activeList = useRef<AbortController | null>(null)
  const activeTrace = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    activeList.current?.abort()
    activeList.current = controller

    const load = async () => {
      setStatus('loading')
      try {
        const page = await fetchDeployments(
          { limit: PAGE_SIZE, offset, ...(statusFilter ? { status: statusFilter } : {}) },
          controller.signal,
        )
        if (controller.signal.aborted) return
        setDeployments(page.items)
        setTotal(page.total)
        setStatus('ready')
        setError(null)
      } catch (cause) {
        if (controller.signal.aborted) return
        setDeployments([])
        setTotal(0)
        setStatus('error')
        setError(cause instanceof Error ? cause.message : 'Unknown error')
      }
    }

    void load()
    return () => controller.abort()
  }, [offset, statusFilter, nonce])

  const select = useCallback((deploymentId: string | null) => {
    if (deploymentId === null) {
      activeTrace.current?.abort()
      setSelected(null)
      setTraceError(null)
      return
    }

    const controller = new AbortController()
    activeTrace.current?.abort()
    activeTrace.current = controller

    const load = async () => {
      setTraceState('loading')
      setTraceError(null)
      try {
        const detail = await fetchDeployment(deploymentId, controller.signal)
        if (controller.signal.aborted) return
        setSelected(detail)
        setTraceState('ready')
      } catch (cause) {
        if (controller.signal.aborted) return
        setSelected(null)
        setTraceState('error')
        setTraceError(cause instanceof Error ? cause.message : 'Unknown error')
      }
    }

    void load()
  }, [])

  const setStatusFilter = useCallback((next: DeploymentStatus | null) => {
    // A filter change invalidates the page: staying on page four of a new filter
    // would usually show nothing and read as "no deployments".
    setOffset(0)
    setStatusFilterState(next)
  }, [])

  const page = useCallback((next: number) => {
    if (next < 0) return
    setOffset(next)
  }, [])

  const retry = useCallback(() => setNonce((value) => value + 1), [])

  return {
    deployments,
    total,
    offset,
    status,
    error,
    selected,
    traceState,
    traceError,
    statusFilter,
    setStatusFilter,
    select,
    page,
    retry,
  }
}