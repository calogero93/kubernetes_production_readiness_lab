import { describe, expect, it } from 'vitest'
import { examplePlan, parsePlan } from './planRequest'

describe('composed plans', () => {
  it('leaves the default plan to the backend when no plan is provided', () => {
    expect(parsePlan('  ')).toBeNull()
  })
  it('preserves parameters and dependencies of the composition', () => {
    expect(parsePlan(JSON.stringify(examplePlan))).toEqual(examplePlan)
    expect(examplePlan.tasks.filter((task) => task.capability === 'http_service')).toHaveLength(2)
  })
  it('rejects unusable input before uploading a chart', () => {
    for (const value of ['{', 'null', '{}', '{"tasks": []}']) {
      expect(() => parsePlan(value)).toThrow()
    }
  })
})
