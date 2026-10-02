import { BackendConnection } from './components/BackendConnection'
import { ComponentList } from './components/ComponentList'
import { SystemStatus } from './components/SystemStatus'
import { useHealth } from './hooks/useHealth'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

export default function App() {
  const { connection, health, error, latencyMs, lastChecked, refresh } = useHealth()

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead__brand">
          <span className="masthead__mark" aria-hidden="true" />
          <div>
            <h1 className="masthead__title">Aegis</h1>
            <p className="masthead__tagline">Autonomous AI DevOps Engineer</p>
          </div>
        </div>
        <span className="masthead__stage">{health?.stage ?? 'stage-1-foundation'}</span>
      </header>

      <main className="grid">
        <SystemStatus health={health} loading={connection === 'connecting'} />
        <BackendConnection
          connection={connection}
          endpoint={`${API_BASE_URL}/health`}
          latencyMs={latencyMs}
          lastChecked={lastChecked}
          error={error}
          onRefresh={refresh}
        />
        {health && health.components.length > 0 && (
          <div className="grid__full">
            <ComponentList components={health.components} />
          </div>
        )}
      </main>

      <footer className="footer">
        <p>
          Foundation stage. Repository analysis, deployment planning and MCP tool
          integration arrive in later stages.
        </p>
      </footer>
    </div>
  )
}