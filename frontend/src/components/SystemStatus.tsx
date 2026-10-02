import { StatusCard } from './StatusCard'
import { toneForStatus } from './tone'
import type { HealthResponse } from '../types/health'
import { STATUS_LABELS } from '../types/health'

interface SystemStatusProps {
  health: HealthResponse | null
  loading: boolean
}

/** Format a second count as a compact human duration. */
function formatUptime(seconds: number): string {
  const total = Math.floor(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60

  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${secs}s`
  return `${secs}s`
}

/** Overall service health reported by the backend. */
export function SystemStatus({ health, loading }: SystemStatusProps) {
  const tone = loading ? 'muted' : health ? toneForStatus(health.status) : 'error'
  const label = loading
    ? 'Checking…'
    : health
      ? STATUS_LABELS[health.status]
      : 'Unavailable'

  return (
    <StatusCard
      title="System Status"
      subtitle="Aggregate health of the Aegis backend"
      tone={tone}
      statusLabel={label}
    >
      <dl className="metrics">
        <div className="metric">
          <dt>Service</dt>
          <dd>{health?.service ?? '—'}</dd>
        </div>
        <div className="metric">
          <dt>Version</dt>
          <dd>{health?.version ?? '—'}</dd>
        </div>
        <div className="metric">
          <dt>Environment</dt>
          <dd>{health?.environment ?? '—'}</dd>
        </div>
        <div className="metric">
          <dt>Uptime</dt>
          <dd>{health ? formatUptime(health.uptime_seconds) : '—'}</dd>
        </div>
        <div className="metric">
          <dt>Stage</dt>
          <dd>{health?.stage ?? '—'}</dd>
        </div>
      </dl>
    </StatusCard>
  )
}