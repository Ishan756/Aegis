import type { DeploymentStatus } from '../types/deployment'
import type { ComponentStatus } from '../types/health'

/** Visual tones shared by dashboard components. */
export type Tone = 'ok' | 'warn' | 'muted' | 'error'

/** Map a backend status value onto a visual tone. */
export function toneForStatus(status: ComponentStatus): Tone {
  switch (status) {
    case 'ok':
      return 'ok'
    case 'degraded':
      return 'warn'
    case 'not_configured':
      return 'muted'
    case 'error':
      return 'error'
  }
}

/**
 * Map a deployment status onto a visual tone.
 *
 * `in_progress` and `dry_run` are muted rather than ok: neither is a success,
 * and colouring them green would let a dashboard read as healthy while a
 * deployment is still in flight or never deployed anything.
 */
export function toneForDeployment(status: DeploymentStatus): Tone {
  switch (status) {
    case 'succeeded':
      return 'ok'
    case 'degraded':
      return 'warn'
    case 'failed':
      return 'error'
    case 'in_progress':
    case 'dry_run':
      return 'muted'
  }
}