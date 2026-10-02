import { StatusCard } from './StatusCard'
import { toneForStatus } from './tone'
import type { ComponentHealth } from '../types/health'
import { STATUS_LABELS } from '../types/health'

interface ComponentListProps {
  components: ComponentHealth[]
}

/**
 * Per-subsystem breakdown returned by the health endpoint.
 *
 * Subsystems that are not implemented yet are listed as "Not configured" so
 * the roadmap is visible in the product itself.
 */
export function ComponentList({ components }: ComponentListProps) {
  const ready = components.filter((component) => component.status === 'ok').length
  const tone = components.some((component) => component.status === 'error')
    ? 'error'
    : components.every((component) => component.status === 'ok')
      ? 'ok'
      : 'warn'

  return (
    <StatusCard
      title="Components"
      subtitle="Subsystems reported by the health endpoint"
      tone={tone}
      statusLabel={`${ready}/${components.length} operational`}
    >
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Component</th>
            <th scope="col">Status</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {components.map((component) => (
            <tr key={component.name}>
              <th scope="row" className="metric__mono">
                {component.name}
              </th>
              <td>
                <span className={`badge badge--${toneForStatus(component.status)}`}>
                  <span className="badge__dot" aria-hidden="true" />
                  {STATUS_LABELS[component.status]}
                </span>
              </td>
              <td className="table__detail">{component.detail ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </StatusCard>
  )
}