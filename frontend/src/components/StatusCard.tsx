import type { ReactNode } from 'react'

import type { Tone } from './tone'

interface StatusCardProps {
  title: string
  subtitle?: string
  tone?: Tone
  statusLabel?: string
  actions?: ReactNode
  children?: ReactNode
}

/** Card container used across the dashboard. */
export function StatusCard({
  title,
  subtitle,
  tone = 'muted',
  statusLabel,
  actions,
  children,
}: StatusCardProps) {
  return (
    <section className={`card card--${tone}`}>
      <header className="card__header">
        <div>
          <h2 className="card__title">{title}</h2>
          {subtitle && <p className="card__subtitle">{subtitle}</p>}
        </div>
        <div className="card__actions">
          {actions}
          {statusLabel && (
            <span className={`badge badge--${tone}`}>
              <span className="badge__dot" aria-hidden="true" />
              {statusLabel}
            </span>
          )}
        </div>
      </header>
      {children}
    </section>
  )
}