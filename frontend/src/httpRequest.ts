import type { HttpProbeOptions } from './types'

export type HttpProbeDraft = Omit<HttpProbeOptions, 'max_p95_ms'> & {
  enabled: boolean
  max_p95_ms: string
}

export const defaultHttpProbe: HttpProbeDraft = {
  enabled: false,
  service: '',
  port: 80,
  path: '/healthz',
  requests: 10,
  expected_status: 200,
  max_failed_requests: 0,
  max_p95_ms: '',
}

export function buildHttpProbe(draft: HttpProbeDraft): HttpProbeOptions | null {
  if (!draft.enabled) return null
  const { enabled: _, max_p95_ms, ...options } = draft
  const p95 = max_p95_ms.trim() ? Number(max_p95_ms) : null
  if (!/^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$/.test(options.service) || options.service.length > 63) {
    throw new Error('Select the name of a rendered Service in the product namespace.')
  }
  if (!/^\/[a-zA-Z0-9/_.~-]*$/.test(options.path) || options.path.includes('//') || options.path.length > 256) {
    throw new Error('HTTP path must start with / and have no query string or //.')
  }
  const bounded = (value: number, min: number, max: number) => Number.isInteger(value) && value >= min && value <= max
  if (!bounded(options.port, 1, 65535) || !bounded(options.requests, 1, 50) ||
    !bounded(options.expected_status, 100, 599) || !bounded(options.max_failed_requests, 0, options.requests - 1)) {
    throw new Error('Check the HTTP port, request count, expected status and failure allowance.')
  }
  if (p95 !== null && (!Number.isFinite(p95) || p95 <= 0)) {
    throw new Error('Maximum p95 must be a positive number, or left empty.')
  }
  return { ...options, max_p95_ms: p95 }
}
