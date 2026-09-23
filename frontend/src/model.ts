import type { EvaluationSummary, ResourceRef } from './types'

export type Filter = 'all' | 'blockers' | 'warnings' | 'missing'

export function summarize(items: EvaluationSummary[]) {
  return {
    total: items.length,
    blocked: items.filter((item) => item.blocker_count > 0).length,
    warnings: items.reduce((total, item) => total + item.warning_count, 0),
    missing: items.filter((item) => item.bundle_status === 'missing').length,
  }
}

export function filterEvaluations(items: EvaluationSummary[], query: string, filter: Filter) {
  const normalized = query.trim().toLocaleLowerCase()
  return items.filter((item) => {
    const matchesFilter =
      filter === 'all' ||
      (filter === 'blockers' && item.blocker_count > 0) ||
      (filter === 'warnings' && item.warning_count > 0) ||
      (filter === 'missing' && item.bundle_status === 'missing')
    const matchesQuery =
      !normalized ||
      [item.chart, item.profile_name, item.evaluation_id].some((value) =>
        value.toLocaleLowerCase().includes(normalized),
      )
    return matchesFilter && matchesQuery
  })
}

export function resourceLabel(resource: ResourceRef | null): string {
  if (!resource) return 'Evaluation'
  const scopedName = resource.namespace ? `${resource.namespace}/${resource.name}` : resource.name
  return `${resource.kind} ${scopedName}${resource.container ? ` · ${resource.container}` : ''}`
}

export function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
