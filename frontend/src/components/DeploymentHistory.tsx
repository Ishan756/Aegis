import { StatusCard } from './StatusCard'
import { toneForDeployment } from './tone'
import { DEPLOYMENT_STATUS_LABELS } from '../types/deployment'
import type { DeploymentStatus, DeploymentSummary } from '../types/deployment'

interface DeploymentHistoryProps {
  deployments: DeploymentSummary[]
  total: number
  offset: number
  loading: boolean
  error: string | null
  statusFilter: DeploymentStatus | null
  onStatusFilter: (status: DeploymentStatus | null) => void
  onSelect: (deploymentId: string) => void
  onPage: (offset: number) => void
  onRetry: () => void
}

/** Short commit label: seven characters is what a log line shows. */
function shortCommit(sha: string | null, ref: string | null): string {
  if (sha) return sha.slice(0, 7)
  return ref ?? '—'
}

/** Relative time, or nothing for a null timestamp. */
function relativeTime(value: string | null, now: Date): string {
  if (!value) return '—'
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return '—'

  const seconds = Math.round((now.getTime() - then) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

/**
 * Deployment history list.
 *
 * Newest first, matching the backend's ordering, because the run somebody wants
 * is almost always the last one. Failures are surfaced in their own column and
 * in the row status rather than only in the trace, so a scan of the page tells
 * you whether anything is broken without opening anything.
 */
export function DeploymentHistory({
  deployments,
  total,
  offset,
  loading,
  error,
  statusFilter,
  onStatusFilter,
  onSelect,
  onPage,
  onRetry,
}: DeploymentHistoryProps) {
  const now = new Date()
  const failed = deployments.filter((d) => d.status === 'failed').length
  const tone = failed > 0 ? 'error' : loading ? 'muted' : 'ok'

  const pageSize = deployments.length > 0 ? deployments.length : 1
  const canPageForward = offset + pageSize < total
  const canPageBack = offset > 0

  return (
    <StatusCard
      title="Deployment History"
      subtitle="Every recorded run, newest first"
      tone={tone}
      statusLabel={loading ? 'Loading' : `${total} recorded`}
      actions={
        <select
          className="select"
          aria-label="Filter by status"
          value={statusFilter ?? ''}
          onChange={(event) =>
            onStatusFilter((event.target.value || null) as DeploymentStatus | null)
          }
        >
          <option value="">All statuses</option>
          {(Object.keys(DEPLOYMENT_STATUS_LABELS) as DeploymentStatus[]).map((value) => (
            <option key={value} value={value}>
              {DEPLOYMENT_STATUS_LABELS[value]}
            </option>
          ))}
        </select>
      }
    >
      {error && (
        <p className="note note--error" role="alert">
          {error}{' '}
          <button type="button" className="button" onClick={onRetry}>
            Retry
          </button>
        </p>
      )}

      {!error && loading && deployments.length === 0 && (
        <p className="note">Loading deployment history…</p>
      )}

      {!error && !loading && deployments.length === 0 && (
        <p className="note">
          No deployments recorded yet. A run appears here as soon as one is recorded.
        </p>
      )}

      {deployments.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Repository</th>
              <th scope="col">Commit</th>
              <th scope="col">Image</th>
              <th scope="col">Actions</th>
              <th scope="col">Failures</th>
              <th scope="col">When</th>
            </tr>
          </thead>
          <tbody>
            {deployments.map((deployment) => (
              <tr key={deployment.deployment_id}>
                <th scope="row">
                  <button
                    type="button"
                    className="link"
                    onClick={() => onSelect(deployment.deployment_id)}
                    aria-label={`Open trace for ${deployment.deployment_id}`}
                  >
                    <span className={`badge badge--${toneForDeployment(deployment.status)}`}>
                      <span className="badge__dot" aria-hidden="true" />
                      {DEPLOYMENT_STATUS_LABELS[deployment.status]}
                    </span>
                  </button>
                </th>
                <td className="metric__mono">
                  {deployment.repository ?? deployment.repository_path ?? '—'}
                </td>
                <td className="metric__mono">
                  {shortCommit(deployment.commit_sha, deployment.commit_ref)}
                </td>
                <td className="metric__mono">{deployment.image ?? '—'}</td>
                <td>{deployment.action_count}</td>
                <td className={deployment.failure_count > 0 ? 'note--error' : undefined}>
                  {deployment.failure_count}
                </td>
                <td className="table__detail">
                  {relativeTime(deployment.started_at, now)}
                  {deployment.recovered && (
                    <>
                      {' '}
                      <span className="badge badge--ok">recovered</span>
                    </>
                  )}
                  {deployment.dry_run && (
                    <>
                      {' '}
                      <span className="badge badge--muted">dry run</span>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {total > 0 && (
        <nav className="pager" aria-label="Deployment history pages">
          <button
            type="button"
            className="button"
            disabled={!canPageBack}
            onClick={() => onPage(Math.max(0, offset - pageSize))}
          >
            Previous
          </button>
          <span className="note">
            {Math.min(offset + 1, total)}–{Math.min(offset + pageSize, total)} of {total}
          </span>
          <button
            type="button"
            className="button"
            disabled={!canPageForward}
            onClick={() => onPage(offset + pageSize)}
          >
            Next
          </button>
        </nav>
      )}
    </StatusCard>
  )
}