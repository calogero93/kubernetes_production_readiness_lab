import type { EvaluationPlan } from './types'

export const examplePlan = {
  schema_version: '1', catalog_version: '1', origin: 'user',
  objective: 'Check HTTP behavior and inspect product resource usage.',
  budget: { max_tasks: 8, max_elapsed_seconds: 300, max_parallel_tasks: 2 },
  tasks: [
    { id: 'http', capability: 'http_service', rationale: 'Verify the public endpoint.', parameters: { service: 'kubeproof-target', port: 8080, path: '/', requests: 10, expected_status: 200, max_failed_requests: 0, max_p95_ms: 500 } },
    { id: 'resources', capability: 'resource_sample', rationale: 'Observe usage during the HTTP probe.', parameters: { duration_seconds: 30 } },
    { id: 'after-http', capability: 'http_service', depends_on: ['http', 'resources'], when: 'completed', rationale: 'Verify the endpoint after the observation window.', parameters: { service: 'kubeproof-target', port: 8080, path: '/healthz', requests: 5, expected_status: 200, max_failed_requests: 0 } },
  ],
} satisfies EvaluationPlan

export function parsePlan(text: string): EvaluationPlan | null {
  if (!text.trim()) return null
  let value: unknown
  try { value = JSON.parse(text) } catch { throw new Error('Plan must be valid JSON.') }
  if (!value || typeof value !== 'object' || !('tasks' in value) || !Array.isArray(value.tasks) || value.tasks.length === 0) {
    throw new Error('Plan must contain a nonempty tasks array.')
  }
  // The backend validates the catalog, typed parameters, budget and dependency graph.
  return value as EvaluationPlan
}
