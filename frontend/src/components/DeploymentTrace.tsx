import { StatusCard } from './StatusCard'
import { toneForDeployment } from './tone'
import { FAILURE_STAGE_LABELS } from '../types/deployment'
import type { DeploymentDetail, FailureRecord } from '../types/deployment'

interface DeploymentTraceProps {
  detail: DeploymentDetail
  onClose: () => void
}

/** Seconds, or a dash. Avoids rendering "0.00s" for a check that never ran. */
function duration(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value.toFixed(2)}s`
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

/** Tone a failure by stage: recovery and verification problems are the loudest. */
function failureTone(failure: FailureRecord): string {
  return failure.stage === 'recover' || failure.stage === 'verify' ? 'error' : 'warn'
}

/**
 * One deployment's execution trace.
 *
 * Ordered the way the run happened — plan, actions, verification, failures,
 * recovery — rather than grouped by data type. A trace is read to answer "what
 * happened and when", and an answer that requires reassembling four lists defeats
 * the purpose of keeping the record.
 *
 * Failures come first when there are any. Somebody opening a failed deployment
 * wants the cause; somebody opening a successful one wants confirmation, and
 * will scroll past an empty failure list anyway.
 */
export function DeploymentTrace({ detail, onClose }: DeploymentTraceProps) {
  const tone = toneForDeployment(detail.status)
  const escalated = detail.escalated || detail.failures.length > 0

  return (
    <StatusCard
      title={`Deployment ${detail.deployment_id}`}
      subtitle={
        detail.repository ??
        detail.repository_path ??
        detail.image ??
        'Recorded run'
      }
      tone={tone}
      statusLabel={
        detail.recovered
          ? 'Succeeded after recovery'
          : detail.dry_run
            ? 'Dry run'
            : undefined
      }
      actions={
        <button type="button" className="button" onClick={onClose}>
          Close
        </button>
      }
    >
      <dl className="metrics">
        <div className="metric">
          <dt>Commit</dt>
          <dd className="metric__mono">{detail.commit_sha ?? detail.commit_ref ?? '—'}</dd>
        </div>
        <div className="metric">
          <dt>Image</dt>
          <dd className="metric__mono">{detail.image ?? '—'}</dd>
        </div>
        <div className="metric">
          <dt>Container</dt>
          <dd className="metric__mono">{detail.container ?? '—'}</dd>
        </div>
        <div className="metric">
          <dt>Started</dt>
          <dd>{formatTime(detail.started_at)}</dd>
        </div>
        <div className="metric">
          <dt>Duration</dt>
          <dd>{duration(detail.duration_seconds)}</dd>
        </div>
      </dl>

      {detail.escalation_reason && (
        <p className="note note--error" role="alert">
          Escalated: {detail.escalation_reason}
        </p>
      )}
      {detail.error && (
        <p className="note note--error" role="alert">
          {detail.error}
        </p>
      )}

      {escalated && (
        <section className="trace__section">
          <h3 className="trace__heading">Failures</h3>
          {detail.failures.length === 0 ? (
            <p className="note">None recorded.</p>
          ) : (
            <ul className="trace__list">
              {detail.failures.map((failure, index) => (
                <li key={`${failure.stage}-${failure.kind}-${index}`}>
                  <span className={`badge badge--${failureTone(failure)}`}>
                    {FAILURE_STAGE_LABELS[failure.stage]}
                  </span>{' '}
                  <span className="metric__mono">{failure.kind}</span>
                  <span className="trace__message"> — {failure.message}</span>
                  {failure.tool && (
                    <div className="table__detail metric__mono">tool: {failure.tool}</div>
                  )}
                  {failure.task_id && (
                    <div className="table__detail metric__mono">
                      task: {failure.task_id}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <section className="trace__section">
        <h3 className="trace__heading">Actions ({detail.actions.length})</h3>
        {detail.actions.length === 0 ? (
          <p className="note">No actions were taken.</p>
        ) : (
          <table className="table" aria-label="Actions">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Tool</th>
                <th scope="col">Status</th>
                <th scope="col">Duration</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {detail.actions.map((action) => (
                <tr key={action.sequence}>
                  <th scope="row">{action.sequence}</th>
                  <td className="metric__mono">{action.tool}</td>
                  <td>
                    <span
                      className={`badge badge--${action.status === 'success' ? 'ok' : action.status === 'failed' ? 'error' : 'muted'}`}
                    >
                      {action.status}
                    </span>
                  </td>
                  <td className="metric__mono">{duration(action.duration_seconds)}</td>
                  <td className="table__detail">
                    {action.error ?? action.result_summary ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="trace__section">
        <h3 className="trace__heading">Verification</h3>
        {detail.verification === null ? (
          <p className="note">Not run.</p>
        ) : (
          <>
            <p className="note">
              <strong>{detail.verification.status}</strong>
              {detail.verification.container && (
                <>
                  {' '}
                  on <span className="metric__mono">{detail.verification.container}</span>
                </>
              )}
            </p>
            {detail.verification.checks.length > 0 && (
              <ul className="trace__list">
                {detail.verification.checks.map((check) => (
                  <li key={check.name}>
                    <span className="metric__mono">{check.name}</span>
                    {' — '}
                    {check.outcome}
                    {check.detail && <span className="trace__message"> ({check.detail})</span>}
                    {check.skipped_reason && (
                      <div className="table__detail">skipped: {check.skipped_reason}</div>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {detail.verification.failures.length > 0 && (
              <ul className="trace__list">
                {detail.verification.failures.map((failure, index) => (
                  <li key={index} className="note--error">
                    {failure}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      {detail.recovery_attempts.length > 0 && (
        <section className="trace__section">
          <h3 className="trace__heading">Recovery attempts ({detail.recovery_attempts.length})</h3>
          <ul className="trace__list">
            {detail.recovery_attempts.map((attempt, index) => (
              <li key={index}>
                <span className="metric__mono">{attempt.action ?? 'unknown'}</span>
                <span className="trace__message"> — {attempt.description ?? ''}</span>
                <div className="table__detail">
                  applied={String(attempt.applied)} succeeded={String(attempt.succeeded)}
                  {attempt.risk && ` · risk: ${attempt.risk}`}
                </div>
                {attempt.declined_reason && (
                  <div className="table__detail">declined: {attempt.declined_reason}</div>
                )}
                {attempt.error && <div className="note note--error">{attempt.error}</div>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {detail.tasks.length > 0 && (
        <details className="trace__section">
          <summary className="trace__heading">Planned tasks ({detail.tasks.length})</summary>
          <table className="table" aria-label="Planned tasks">
            <thead>
              <tr>
                <th scope="col">Task</th>
                <th scope="col">Tool</th>
                <th scope="col">Title</th>
              </tr>
            </thead>
            <tbody>
              {detail.tasks.map((task, index) => (
                <tr key={index}>
                  <th scope="row" className="metric__mono">
                    {String(task.id ?? index + 1)}
                  </th>
                  <td className="metric__mono">{String(task.tool ?? '—')}</td>
                  <td>{String(task.title ?? '—')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {detail.lessons.length > 0 && (
        <section className="trace__section">
          <h3 className="trace__heading">Lessons ({detail.lessons.length})</h3>
          <ul className="trace__list">
            {detail.lessons.map((lesson) => (
              <li key={lesson.fingerprint}>
                <span
                  className={`badge badge--${lesson.severity === 'critical' ? 'error' : lesson.severity === 'warning' ? 'warn' : 'muted'}`}
                >
                  {lesson.severity}
                </span>{' '}
                <strong>{lesson.title}</strong>
                <span className="table__detail"> · seen {lesson.occurrences}x</span>
                <div className="trace__message">{lesson.summary}</div>
                {lesson.recommendation && (
                  <div className="table__detail">Recommendation: {lesson.recommendation}</div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </StatusCard>
  )
}