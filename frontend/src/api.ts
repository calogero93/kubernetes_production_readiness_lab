import type { Evaluation, EvaluationSummary } from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as { error?: string }
      if (body.error) message = body.error
    } catch {
      // The HTTP status remains actionable if the response is not JSON.
    }
    throw new Error(message)
  }
  return (await response.json()) as T
}

export const listEvaluations = () => request<EvaluationSummary[]>('/api/evaluations')

export const getEvaluation = (id: string) =>
  request<Evaluation>(`/api/evaluations/${encodeURIComponent(id)}`)

export const syncEvaluations = () =>
  request<{ indexed: number }>('/api/sync', { method: 'POST' })
