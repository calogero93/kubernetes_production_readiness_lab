export type CpuRequestInput = {
  targetRps: number
  maxP95Ms: number
  maxFailed: number
  maxCpu: number
  iterations: number
  useAi: boolean
}

export const defaultProfile = {
  schema_version: '0.1',
  name: 'ui-cpu-sandbox',
  constraints: {
    security: {
      allow_cluster_admin: false,
      allow_wildcard_rbac: false,
      allow_privileged: false,
      allow_host_network: false,
      allow_host_pid: false,
      allow_host_path: false,
      require_run_as_non_root: true,
      require_read_only_root_filesystem: true,
      allowed_added_capabilities: [],
    },
    resources: {
      require_requests: true,
      require_limits: true,
      max_memory_limit_per_pod: '2Gi',
      max_cpu_limit_per_pod: '2',
      max_sampled_memory_per_pod: '2Gi',
      max_sampled_cpu_per_pod: '2',
    },
    reliability: { minimum_replicas: 2 },
    networking: { allow_public_egress: false, allowed_external_domains: [] },
  },
}

export function buildCpuRequest(input: CpuRequestInput) {
  return {
    schema_version: '1',
    environment_id: 'local-kind',
    target_id: 'cpu-fixture',
    work_iterations: input.iterations,
    goal: {
      target_rps: input.targetRps,
      max_p95_ms: input.maxP95Ms,
      max_failed_requests: input.maxFailed,
      max_cpu_millicores: input.maxCpu,
    },
    budget: {
      max_plan_versions: input.useAi ? 5 : 1,
      max_elapsed_seconds: input.useAi ? 2400 : 1200,
      max_model_calls: input.useAi ? 6 : 1,
      max_tool_calls: input.useAi ? 15 : 3,
      max_requests_per_trial: 10_000,
    },
  }
}
