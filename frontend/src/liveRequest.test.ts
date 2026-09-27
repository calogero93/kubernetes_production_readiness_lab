import { describe, expect, it } from 'vitest'

import { buildCpuRequest } from './liveRequest'

const input = {
  targetRps: 5,
  maxP95Ms: 500,
  maxFailed: 0,
  maxCpu: 500,
  iterations: 20_000,
  useAi: false,
}

describe('CPU run request', () => {
  it('keeps the confirmed goal independent of the planning mode', () => {
    const pilot = buildCpuRequest(input)
    const ai = buildCpuRequest({ ...input, useAi: true })

    expect(pilot.goal).toEqual(ai.goal)
    expect(pilot.work_iterations).toBe(20_000)
    expect(pilot.environment_id).toBe('local-kind')
    expect(pilot.target_id).toBe('cpu-fixture')
  })

  it('gives the AI supervisor a larger but bounded budget', () => {
    expect(buildCpuRequest(input).budget).toEqual({
      max_plan_versions: 1,
      max_elapsed_seconds: 1200,
      max_model_calls: 1,
      max_tool_calls: 3,
      max_requests_per_trial: 10_000,
    })
    expect(buildCpuRequest({ ...input, useAi: true }).budget).toEqual({
      max_plan_versions: 5,
      max_elapsed_seconds: 2400,
      max_model_calls: 6,
      max_tool_calls: 15,
      max_requests_per_trial: 10_000,
    })
  })
})
