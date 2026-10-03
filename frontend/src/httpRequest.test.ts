import { describe, expect, it } from 'vitest'

import { buildHttpProbe, defaultHttpProbe } from './httpRequest'

describe('confirmed HTTP Service probe', () => {
  it('leaves HTTP probing off unless explicitly selected', () => {
    expect(buildHttpProbe(defaultHttpProbe)).toBeNull()
  })

  it('keeps the reviewed target, thresholds and optional p95 in the submitted request', () => {
    expect(buildHttpProbe({ ...defaultHttpProbe, enabled: true, service: 'api', port: 8080, max_p95_ms: '150.5' })).toEqual({
      service: 'api', port: 8080, path: '/healthz', requests: 10,
      expected_status: 200, max_failed_requests: 0, max_p95_ms: 150.5,
    })
    expect(buildHttpProbe({ ...defaultHttpProbe, enabled: true, service: 'api' })?.max_p95_ms).toBeNull()
  })

  it.each([
    { service: 'https://external.example' },
    { path: '//external.example' },
    { path: '/healthz?token=secret' },
    { requests: 51 },
    { max_failed_requests: 10 },
    { max_p95_ms: 'NaN' },
    { port: 0 },
  ])('rejects an unsupported or unbounded request: %j', (change) => {
    expect(() => buildHttpProbe({ ...defaultHttpProbe, enabled: true, service: 'api', ...change })).toThrow()
  })
})
