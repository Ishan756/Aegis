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