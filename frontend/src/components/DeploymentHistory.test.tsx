import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

import { DeploymentHistory } from './DeploymentHistory'
import { DeploymentTrace } from './DeploymentTrace'
import type {
  DeploymentDetail,
  DeploymentStatus,
  DeploymentSummary,
} from '../types/deployment'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function summary(overrides: Partial<DeploymentSummary> = {}): DeploymentSummary {
  return {
    deployment_id: 'dep-1',
    repository: 'acme/api',
    repository_path: '../examples/sample_app',
    commit_sha: 'abcdef1234567890',
    commit_ref: 'main',
    image: 'acme/api:dev',
    container: 'acme-api',
    status: 'succeeded',
    recovered: false,
    escalated: false,
    escalation_reason: null,
    dry_run: false,
    action_count: 3,
    failure_count: 0,
    // Fixed clock so relative timestamps are assertions, not guesses.
    started_at: '2026-01-01T12:00:00Z',
    finished_at: '2026-01-01T12:00:12Z',
    duration_seconds: 12,
    ...overrides,
  }
}

function detail(overrides: Partial<DeploymentDetail> = {}): DeploymentDetail {
  return {
    ...summary(),
    run_id: 'run-1',
    error: null,
    plan: null,
    tasks: [],
    actions: [],
    verification: null,
    failures: [],
    recovery_attempts: [],
    incident: null,
    request: null,
    created_at: '2026-01-01T12:00:00Z',
    updated_at: '2026-01-01T12:00:12Z',
    lessons: [],
    ...overrides,
  }
}

function renderHistory(props: Partial<React.ComponentProps<typeof DeploymentHistory>> = {}) {
  const handlers = {
    onStatusFilter: vi.fn(),
    onSelect: vi.fn(),
    onPage: vi.fn(),
    onRetry: vi.fn(),
  }
  const { container } = render(
    <DeploymentHistory
      deployments={[]}
      total={0}
      offset={0}
      loading={false}
      error={null}
      statusFilter={null}
      {...handlers}
      {...props}
    />,
  )
  return { ...handlers, container }
}

describe('DeploymentHistory', () => {
  it('says so when nothing has been recorded', () => {
    renderHistory()

    expect(screen.getByText(/No deployments recorded yet/)).toBeTruthy()
  })

  it('renders one row per deployment with its status', () => {
    renderHistory({
      deployments: [
        summary({ deployment_id: 'dep-1' }),
        summary({ deployment_id: 'dep-2', status: 'failed' as DeploymentStatus }),
      ],
      total: 2,
    })

    const rows = screen.getByRole('table')
    expect(within(rows).getByText('Succeeded')).toBeTruthy()
    expect(within(rows).getByText('Failed')).toBeTruthy()
    expect(screen.getByText('2 recorded')).toBeTruthy()
  })

  it('shows the short commit, not the whole sha', () => {
    renderHistory({ deployments: [summary()], total: 1 })

    expect(screen.getByText('abcdef1')).toBeTruthy()
    expect(screen.queryByText('abcdef1234567890')).toBeNull()
  })

  it('falls back to the ref when there is no commit', () => {
    renderHistory({
      deployments: [summary({ commit_sha: null, commit_ref: 'release/2.1' })],
      total: 1,
    })

    expect(screen.getByText('release/2.1')).toBeTruthy()
  })

  it('surfaces failure counts rather than hiding them', () => {
    renderHistory({
      deployments: [summary({ status: 'failed', failure_count: 4 })],
      total: 1,
    })

    expect(screen.getByText('4')).toBeTruthy()
  })

  it('marks a recovered deployment as recovered', () => {
    renderHistory({
      deployments: [summary({ status: 'succeeded', recovered: true })],
      total: 1,
    })

    expect(screen.getByText('recovered')).toBeTruthy()
  })

  it('marks a dry run as a dry run', () => {
    renderHistory({ deployments: [summary({ status: 'dry_run', dry_run: true })], total: 1 })

    // Scoped to the row: the status filter also offers a "Dry run" option.
    expect(within(screen.getByRole('table')).getByText('Dry run')).toBeTruthy()
  })

  it('opens a trace when a row is activated', () => {
    const { onSelect } = renderHistory({ deployments: [summary()], total: 1 })

    fireEvent.click(screen.getByRole('button', { name: /Open trace for dep-1/ }))

    expect(onSelect).toHaveBeenCalledWith('dep-1')
  })

  it('reports a load failure and offers a retry', () => {
    const { onRetry } = renderHistory({ error: 'Could not load deployment history' })

    expect(screen.getByRole('alert').textContent).toContain('Could not load deployment history')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('does not page past either end', () => {
    renderHistory({ deployments: [summary()], total: 1, offset: 0 })

    // One row and a total of one: there is no second page to go to.
    expect(screen.getByRole('button', { name: 'Next' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Previous' }).hasAttribute('disabled')).toBe(true)
  })

  it('passes a status filter through when chosen', () => {
    const { onStatusFilter } = renderHistory()

    fireEvent.change(screen.getByLabelText('Filter by status'), {
      target: { value: 'failed' },
    })

    expect(onStatusFilter).toHaveBeenCalledWith('failed')
  })

  it('passes an empty filter through as null', () => {
    const { onStatusFilter } = renderHistory({ statusFilter: 'failed' })

    fireEvent.change(screen.getByLabelText('Filter by status'), {
      target: { value: '' },
    })

    expect(onStatusFilter).toHaveBeenCalledWith(null)
  })
})

describe('DeploymentTrace', () => {
  it('shows the identifying facts of the run', () => {
    render(<DeploymentTrace detail={detail()} onClose={vi.fn()} />)

    expect(screen.getByRole('heading', { name: 'Deployment dep-1' })).toBeTruthy()
    expect(screen.getByText('acme/api:dev')).toBeTruthy()
    expect(screen.getByText('acme-api')).toBeTruthy()
    expect(screen.getByText('12.00s')).toBeTruthy()
  })

  it('lists every action taken', () => {
    render(
      <DeploymentTrace
        detail={detail({
          actions: [
            {
              sequence: 1,
              timestamp: null,
              task_id: 'build',
              task_title: 'Build image',
              tool: 'docker.build_image',
              arguments: null,
              status: 'success',
              duration_seconds: 4.5,
              error: null,
              error_code: null,
              error_class: null,
              retry_count: null,
              attempt: null,
              result_summary: null,
            },
            {
              sequence: 2,
              timestamp: null,
              task_id: 'run',
              task_title: 'Start container',
              tool: 'docker.start_container',
              arguments: null,
              status: 'failed',
              duration_seconds: 1.25,
              error: 'container exited immediately',
              error_code: 'tool_error',
              error_class: null,
              retry_count: 0,
              attempt: 1,
              result_summary: null,
            },
          ],
        })}
        onClose={vi.fn()}
      />,
    )

    const table = screen.getByRole('table', { name: 'Actions' })
    expect(within(table).getByText('docker.build_image')).toBeTruthy()
    expect(within(table).getByText('container exited immediately')).toBeTruthy()
  })

  it('says plainly when no actions ran', () => {
    render(<DeploymentTrace detail={detail()} onClose={vi.fn()} />)

    expect(screen.getByText('No actions were taken.')).toBeTruthy()
  })

  it('reports verification that never ran', () => {
    render(<DeploymentTrace detail={detail()} onClose={vi.fn()} />)

    expect(screen.getByText('Not run.')).toBeTruthy()
  })

  it('shows verification checks and their failures', () => {
    render(
      <DeploymentTrace
        detail={detail({
          status: 'failed',
          verification: {
            status: 'FAILED',
            checks: [
              {
                name: 'http_probe',
                outcome: 'failed',
                detail: 'connection refused',
                evidence: null,
                skipped_reason: null,
              },
            ],
            failures: ['container is not running'],
            warnings: [],
            evidence: null,
            recommendation: null,
            container: 'acme-api',
            verified_at: null,
            duration_seconds: 2,
          },
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByText('http_probe')).toBeTruthy()
    expect(screen.getByText('container is not running')).toBeTruthy()
  })

  it('leads with failures when there are any', () => {
    render(
      <DeploymentTrace
        detail={detail({
          status: 'failed',
          error: 'docker.build_image failed',
          failures: [
            {
              stage: 'execute',
              kind: 'out_of_memory',
              message: 'Killed process 1',
              detail: null,
              task_id: 'build',
              tool: 'docker.build_image',
              occurred_at: null,
            },
          ],
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByRole('alert').textContent).toContain('docker.build_image failed')
    expect(screen.getByText('out_of_memory')).toBeTruthy()
    expect(screen.getByText(/Killed process 1/)).toBeTruthy()
  })

  it('labels a failure by the stage it came from', () => {
    render(
      <DeploymentTrace
        detail={detail({
          status: 'failed',
          failures: [
            {
              stage: 'recover',
              kind: 'restart_failed',
              message: 'start refused',
              detail: null,
              task_id: null,
              tool: 'docker.start_container',
              occurred_at: null,
            },
          ],
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByText('Recover')).toBeTruthy()
  })

  it('shows escalation as an alert rather than a footnote', () => {
    render(
      <DeploymentTrace
        detail={detail({
          status: 'failed',
          escalated: true,
          escalation_reason: 'Fix requires image rebuild, which is not automatic.',
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByRole('alert').textContent).toContain('Fix requires image rebuild')
  })

  it('distinguishes a run that needed recovery from one that did not', () => {
    const { rerender } = render(
      <DeploymentTrace detail={detail({ status: 'succeeded' })} onClose={vi.fn()} />,
    )
    expect(screen.queryByText('Succeeded after recovery')).toBeNull()

    rerender(<DeploymentTrace detail={detail({ status: 'succeeded', recovered: true })} onClose={vi.fn()} />)
    expect(screen.getByText('Succeeded after recovery')).toBeTruthy()
  })

  it('shows recovery attempts including declines', () => {
    render(
      <DeploymentTrace
        detail={detail({
          status: 'failed',
          recovery_attempts: [
            {
              index: 1,
              action: 'rebuild_image',
              category: 'rebuild_image',
              risk: 'high',
              description: 'Rebuild the image',
              applied: false,
              succeeded: false,
              declined_reason: 'Requires approval',
              error: null,
            },
          ],
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByText('rebuild_image')).toBeTruthy()
    expect(screen.getByText(/Requires approval/)).toBeTruthy()
  })

  it('shows the lessons a run produced, with their occurrence count', () => {
    render(
      <DeploymentTrace
        detail={detail({
          status: 'failed',
          lessons: [
            {
              lesson_id: 'lsn_1',
              fingerprint: 'fp1',
              title: 'out_of_memory: application',
              summary: 'The process exceeded its memory limit.',
              detail: null,
              repository: 'acme/api',
              component: 'application',
              cause_id: 'out_of_memory',
              severity: 'critical',
              evidence: ['oom_killed=true'],
              recommendation: 'Raise the memory limit.',
              tags: ['out_of_memory'],
              occurrences: 3,
              deployment_ids: ['dep-1'],
              first_seen_at: null,
              last_seen_at: null,
            },
          ],
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByText('out_of_memory: application')).toBeTruthy()
    expect(screen.getByText(/seen 3x/)).toBeTruthy()
    expect(screen.getByText(/Raise the memory limit/)).toBeTruthy()
  })

  it('closes on request', () => {
    const onClose = vi.fn()
    render(<DeploymentTrace detail={detail()} onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(onClose).toHaveBeenCalledOnce()
  })

  it('survives a record with almost nothing in it', () => {
    render(
      <DeploymentTrace
        detail={detail({
          repository: null,
          repository_path: null,
          commit_sha: null,
          commit_ref: null,
          image: null,
          container: null,
          duration_seconds: null,
          started_at: null,
          finished_at: null,
        })}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Deployment dep-1' })).toBeTruthy()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})