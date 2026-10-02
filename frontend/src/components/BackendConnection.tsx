import type { ConnectionState } from '../hooks/useHealth'
import { StatusCard } from './StatusCard'
import type { Tone } from './tone'

interface BackendConnectionProps {
  connection: ConnectionState
  endpoint: string
  latencyMs: number | null
  lastChecked: Date | null
  error: string | null
  onRefresh: () => void
}

const CONNECTION_COPY: Record<ConnectionState, { tone: Tone; label: string; blurb: string }> = {
  connecting: {
    tone: 'muted',
    label: 'Connecting…',
    blurb: 'Attempting to reach the backend.',
  },
  online: {
    tone: 'ok',
    label: 'Connected',
    blurb: 'Dashboard is receiving live telemetry from the Aegis API.',
  },
  offline: {
    tone: 'error',
    label: 'Disconnected',
    blurb: 'No response from the backend. Start it with `make dev-backend`.',
  },
}

/** Backend reachability, latency and last contact time. */
export function BackendConnection({
  connection,
  endpoint,
  latencyMs,
  lastChecked,
  error,
  onRefresh,
}: BackendConnectionProps) {
  const { tone, label, blurb } = CONNECTION_COPY[connection]

  return (
    <StatusCard
      title="Backend Connection"
      subtitle="Live link to the Aegis FastAPI service"
      tone={tone}
      statusLabel={label}
      actions={
        <button type="button" className="button" onClick={onRefresh}>
          Refresh
        </button>
      }
    >
      <dl className="metrics">
        <div className="metric">
          <dt>Endpoint</dt>
          <dd className="metric__mono">{endpoint}</dd>
        </div>
        <div className="metric">
          <dt>Latency</dt>
          <dd>{latencyMs === null ? '—' : `${latencyMs} ms`}</dd>
        </div>
        <div className="metric">
          <dt>Last checked</dt>
          <dd>{lastChecked ? lastChecked.toLocaleTimeString() : '—'}</dd>
        </div>
      </dl>

      <p className="note">{blurb}</p>
      {error && connection === 'offline' && <p className="note note--error">{error}</p>}
    </StatusCard>
  )
}