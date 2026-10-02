/**
 * Wire contract for the Aegis backend health API.
 *
 * Field names intentionally match the Pydantic schemas in
 * `backend/app/models/health.py`.
 */

export type ComponentStatus = 'ok' | 'degraded' | 'not_configured' | 'error'

export interface ComponentHealth {
  name: string
  status: ComponentStatus
  detail: string | null
}

export interface HealthResponse {
  status: ComponentStatus
  service: string
  version: string
  environment: string
  uptime_seconds: number
  stage: string
  components: ComponentHealth[]
}

/** Human-readable labels for each status value. */
export const STATUS_LABELS: Record<ComponentStatus, string> = {
  ok: 'Operational',
  degraded: 'Degraded',
  not_configured: 'Not configured',
  error: 'Error',
}