import { describe, expect, it } from 'vitest'

import { filterEvaluations, resourceLabel, summarize } from './model'
import type { EvaluationSummary } from './types'

const first: EvaluationSummary = {
  evaluation_id: 'a',
  generated_at: '2026-09-23T10:00:00Z',
  chart: 'cert-manager',
  requested_version: '1.0',
  profile_name: 'strict',
  admission_outcome: 'admit',
  blocker_count: 1,
  warning_count: 2,
  failed_check_count: 1,
  warning_check_count: 0,
  bundle_status: 'available',
}

const second: EvaluationSummary = {
  ...first,
  evaluation_id: 'b',
  chart: 'argo-cd',
  blocker_count: 0,
  warning_count: 0,
  bundle_status: 'missing',
}

describe('evidence explorer model', () => {
  it('summarizes runs without turning missing bundles into blockers', () => {
    expect(summarize([first, second])).toEqual({ total: 2, blocked: 1, warnings: 2, missing: 1 })
  })

  it('filters by status and case-insensitive chart or profile search', () => {
    expect(filterEvaluations([first, second], 'CERT', 'blockers')).toEqual([first])
    expect(filterEvaluations([first, second], 'STRICT', 'missing')).toEqual([second])
    expect(filterEvaluations([first, second], 'other', 'all')).toEqual([])
  })

  it('labels namespaced resources and containers', () => {
    expect(
      resourceLabel({
        api_version: 'apps/v1',
        kind: 'Deployment',
        namespace: 'product',
        name: 'controller',
        container: 'manager',
      }),
    ).toBe('Deployment product/controller · manager')
  })
})
