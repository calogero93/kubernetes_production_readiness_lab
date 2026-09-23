import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { EvaluationDetail } from './EvaluationDetail'
import type { Evaluation } from '../types'

const evaluation: Evaluation = {
  schema_version: '0.2',
  evaluation_id: 'run-1',
  generated_at: '2026-09-23T10:00:00Z',
  tool_version: '0.1.0',
  profile_name: 'strict',
  input: {
    chart: 'demo',
    requested_version: '1.0',
    resolved_version: '1.0',
    chart_package_sha256: 'abc',
    rendered_manifest_sha256: 'def',
    profile_sha256: 'ghi',
  },
  admission: { outcome: 'admit', matched_rule_ids: [], explanation: 'Allowed' },
  checks: [{
    id: 'static.security',
    experiment_version: '1',
    title: 'Security footprint',
    execution_status: 'completed',
    assessment: 'fail',
    observation_ids: ['obs-1'],
    finding_ids: ['finding-1'],
    explanation: null,
  }],
  observations: [{
    id: 'obs-1',
    check_id: 'static.security',
    source_class: 'static_input',
    observation_type: 'security.privileged',
    summary: 'Privileged container observed',
    resource: { api_version: 'apps/v1', kind: 'Deployment', namespace: 'demo', name: 'api', container: 'server' },
    data: { privileged: true },
    provenance: { source_ref: 'input:rendered-manifest', source_sha256: 'def' },
  }],
  findings: [{
    id: 'finding-1',
    check_id: 'static.security',
    severity: 'blocker',
    title: 'Privileged workload',
    description: 'The workload requests privilege.',
    observation_ids: ['obs-1'],
    constraint: 'security.allow_privileged',
    remediation: null,
    limitation: null,
  }],
  environment: null,
}

describe('evaluation detail', () => {
  it('links a check to its finding and supporting observation', () => {
    const html = renderToStaticMarkup(
      <EvaluationDetail evaluation={evaluation} loading={false} error={null} onClose={() => {}} />,
    )
    expect(html).toContain('Security footprint')
    expect(html).toContain('Privileged workload')
    expect(html).toContain('Privileged container observed')
    expect(html).toContain('input:rendered-manifest')
  })

  it('shows an error without inventing an evaluation result', () => {
    const html = renderToStaticMarkup(
      <EvaluationDetail evaluation={null} loading={false} error="Bundle unavailable" onClose={() => {}} />,
    )
    expect(html).toContain('Bundle unavailable')
    expect(html).not.toContain('Security footprint')
  })
})
