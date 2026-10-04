/**
 * Wire contract for the Aegis deployment history API.
 *
 * Field names match the Pydantic schemas in
 * `backend/app/models/deployment_record.py`. Everything the backend marks
 * optional is optional here too: a deployment that failed before it had an image
 * genuinely has no image, and inventing an empty string would hide that.
 */

export type DeploymentStatus =
  | 'in_progress'
  | 'succeeded'
  | 'degraded'
  | 'failed'
  | 'dry_run'

export type FailureStage = 'plan' | 'execute' | 'verify' | 'investigate' | 'recover'

export type LessonSeverity = 'info' | 'warning' | 'critical'

/** A list row. Deliberately narrower than {@link DeploymentDetail}. */
export interface DeploymentSummary {
  deployment_id: string
  repository: string | null
  repository_path: string | null
  commit_sha: string | null
  commit_ref: string | null
  image: string | null
  container: string | null
  status: DeploymentStatus
  recovered: boolean
  escalated: boolean
  escalation_reason: string | null
  dry_run: boolean
  action_count: number
  failure_count: number
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
}

export interface ExecutionAction {
  sequence: number
  timestamp: string | null
  task_id: string | null
  task_title: string | null
  tool: string
  arguments: Record<string, unknown> | null
  status: string
  duration_seconds: number | null
  error: string | null
  error_code: string | null
  error_class: string | null
  retry_count: number | null
  attempt: number | null
  result_summary: string | null
}

export interface VerificationCheck {
  name: string
  outcome: string
  detail: string | null
  evidence: string[] | null
  skipped_reason: string | null
}

export interface VerificationResult {
  status: string
  checks: VerificationCheck[]
  failures: string[]
  warnings: string[]
  evidence: string[] | null
  recommendation: string | null
  container: string | null
  verified_at: string | null
  duration_seconds: number | null
}

export interface FailureRecord {
  stage: FailureStage
  kind: string
  message: string
  detail: string | null
  task_id: string | null
  tool: string | null
  occurred_at: string | null
}

export interface RecoveryAttempt {
  index?: number
  action?: string
  category?: string
  risk?: string
  description?: string
  applied?: boolean
  succeeded?: boolean
  declined_reason?: string | null
  error?: string | null
}

/** A full record. `plan`, `incident` and `request` are opaque on purpose. */
export interface DeploymentDetail extends DeploymentSummary {
  run_id: string | null
  error: string | null
  plan: unknown
  tasks: Array<Record<string, unknown>>
  actions: ExecutionAction[]
  verification: VerificationResult | null
  failures: FailureRecord[]
  recovery_attempts: RecoveryAttempt[]
  incident: unknown
  request: unknown
  created_at: string
  updated_at: string
  lessons: Lesson[]
}

export interface Lesson {
  lesson_id: string
  fingerprint: string
  title: string
  summary: string
  detail: string | null
  repository: string | null
  component: string | null
  cause_id: string | null
  severity: LessonSeverity
  evidence: string[]
  recommendation: string | null
  tags: string[]
  /** How many times this cause has been seen. Rises with every repeat. */
  occurrences: number
  deployment_ids: string[]
  first_seen_at: string | null
  last_seen_at: string | null
}

export interface DeploymentListResponse {
  items: DeploymentSummary[]
  total: number
  limit: number
  offset: number
}

export const DEPLOYMENT_STATUS_LABELS: Record<DeploymentStatus, string> = {
  in_progress: 'In progress',
  succeeded: 'Succeeded',
  degraded: 'Degraded',
  failed: 'Failed',
  dry_run: 'Dry run',
}

export const FAILURE_STAGE_LABELS: Record<FailureStage, string> = {
  plan: 'Plan',
  execute: 'Execute',
  verify: 'Verify',
  investigate: 'Investigate',
  recover: 'Recover',
}