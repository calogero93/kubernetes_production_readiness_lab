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
  admission: { outcome: string; matched_rule_ids: string[]; explanation: string }
  checks: CheckResult[]
  observations: Observation[]
  findings: Finding[]
  environment: { provider: string; kubernetes_server_version: string | null } | null
}
