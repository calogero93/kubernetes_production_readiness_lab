import type { Evaluation } from '../types'

export function PlanView({ evaluation }: { evaluation: Evaluation }) {
  const plan = evaluation.execution_options?.test_plan
  if (!plan) return null
  return <section aria-label="Probe plan">
    <h3>Probe plan · {plan.origin}</h3>
    <p>{plan.objective}</p>
    <p>{plan.tasks.length} tasks · up to {plan.budget.max_parallel_tasks} concurrent tasks · {plan.budget.max_elapsed_seconds}s execution budget. The plan stays fixed throughout this evaluation.</p>
    <ol>{plan.tasks.map((task) => {
      const run = evaluation.plan_execution?.tasks.find((item) => item.task_id === task.id)
      return <li key={task.id}>
        <strong>{task.id}</strong> · {task.capability} · {run?.state ?? 'not executed'}
        <p>{task.rationale}</p>
        {(task.depends_on?.length ?? 0) > 0 && <p>After {task.depends_on?.join(', ')} · condition {task.when ?? 'completed'}</p>}
        {run?.explanation && <p>{run.explanation}</p>}
        <details><summary>Parameters</summary><pre>{JSON.stringify(task.parameters, null, 2)}</pre></details>
      </li>
    })}</ol>
  </section>
}
