export type EvaluationSummary = {
  evaluation_id: string
  generated_at: string
  chart: string
  requested_version: string | null
  profile_name: string
  admission_outcome: string
  blocker_count: number
  warning_count: number
  failed_check_count: number
  warning_check_count: number
  bundle_status: 'available' | 'missing'
}

export type ResourceRef = {
  api_version: string
  kind: string
  namespace: string | null
  name: string
  container: string | null
}

export type Observation = {
  id: string
  check_id: string
  source_class: 'static_input' | 'runtime' | 'documentation'
  observation_type: string
  summary: string
  resource: ResourceRef | null
  data: Record<string, unknown>
  provenance: { source_ref: string; source_sha256: string | null } | null
}

export type Finding = {
  id: string
  check_id: string
  severity: 'blocker' | 'warning' | 'info'
  title: string
  description: string
  observation_ids: string[]
  constraint: string | null
  remediation: string | null
  limitation: string | null
}

export type CheckResult = {
  id: string
  experiment_version: string
  title: string
  execution_status: string
  assessment: string
  observation_ids: string[]
  finding_ids: string[]
  explanation: string | null
}

export type Evaluation = {
  schema_version: string
  evaluation_id: string
  generated_at: string
  tool_version: string
  profile_name: string
  input: {
    chart: string
    requested_version: string | null
    resolved_version: string | null
    chart_package_sha256: string | null
    rendered_manifest_sha256: string
    profile_sha256: string | null
  }
  admission: {
    outcome: string
    matched_rule_ids: string[]
    explanation: string
    approval_scope_sha256?: string | null
    operator_approval?: {
      operator_label: string
      reason: string
      scope_sha256: string
      approved_at: string
    } | null
  }
  checks: CheckResult[]
  observations: Observation[]
  findings: Finding[]
  environment: { provider: string; kubernetes_server_version: string | null } | null
  execution_options?: { namespace: string; http_probe?: HttpProbeOptions | null; test_plan?: EvaluationPlan | null } | null
  plan_execution?: { plan_sha256: string; tasks: { task_id: string; state: string; check_ids: string[]; explanation: string | null; started_at: string | null; completed_at: string | null }[] } | null
  ai_interpretation?: { status: string; points: { kind: string; explanation: string; observation_ids: string[]; check_ids: string[]; suggested_manifest_change: string | null; verification: string | null }[]; limitations: string[]; error: string | null } | null
}

export type EvaluationPlan = {
  schema_version: '1'
  catalog_version: '1'
  origin: 'deterministic' | 'user' | 'ai'
  objective: string
  budget: { max_tasks: number; max_elapsed_seconds: number; max_parallel_tasks: number }
  tasks: { id: string; capability: 'http_service' | 'cpu_load' | 'resource_sample' | 'dns_observation' | 'pod_recovery'; parameters: Record<string, unknown>; depends_on?: string[]; when?: 'completed' | 'passed' | 'failed'; rationale: string }[]
}

export type HttpProbeOptions = {
  service: string
  port: number
  path: string
  requests: number
  expected_status: number
  max_failed_requests: number
  max_p95_ms: number | null
}

export type LiveConfig = {
  available: boolean
  csrf_token: string | null
}

export type LiveJob = {
  id: string
  status: 'prepared' | 'queued' | 'running' | 'completed' | 'error'
  chart_name: string
  chart_sha256: string
  preview: Evaluation
  load_error: string | null
  use_ai: boolean
  profile_name: string
  confirmed_request: {
    work_iterations: number
    goal: {
      target_rps: number
      max_p95_ms: number
      max_failed_requests: number
      max_cpu_millicores: number
    }
    budget: {
      max_plan_versions: number
      max_elapsed_seconds: number
      max_tool_calls: number
      max_requests_per_trial: number
    }
  }
  evaluation_id: string | null
  error: string | null
}

export type ChartJob = {
  id: string
  status: 'prepared' | 'queued' | 'running' | 'completed' | 'error'
  chart_name: string
  chart_sha256: string
  rendered_manifest_sha256: string
  approval_scope_sha256: string | null
  http_probe: HttpProbeOptions | null
  test_plan: EvaluationPlan
  plan_sha256: string
  ai_interpret: boolean
  preview: Evaluation
  operator_approval: Evaluation['admission']['operator_approval']
  evaluation_id: string | null
  error: string | null
}
