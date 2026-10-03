import type { LiveJob } from '../types'
import { PlanView } from './PlanView'

type Props = {
  job: LiveJob
  approved: boolean
  busy: boolean
  onApprovalChange: (approved: boolean) => void
  onStart: () => void
}

export function LiveRunReview({ job, approved, busy, onApprovalChange, onStart }: Props) {
  const { goal, budget } = job.confirmed_request

  return (
    <div className="live-review" aria-live="polite">
      <h3>{job.status === 'prepared' ? 'Review before execution' : 'Run status'}</h3>
      <p><strong>{job.chart_name}</strong> · SHA-256 <code>{job.chart_sha256}</code></p>
      <p>Confirmed profile: <strong>{job.profile_name}</strong> · {job.use_ai ? 'AI supervisor' : 'deterministic pilot'}</p>
      <p>Goal: at least {goal.target_rps} successful req/s, p95 at most {goal.max_p95_ms} ms, at most {goal.max_failed_requests} failures, sampled Pod CPU at most {goal.max_cpu_millicores} mCPU. Work: {job.confirmed_request.work_iterations} iterations/request.</p>
      <p>Confirmed limits: {budget.max_tool_calls} tool calls, {budget.max_requests_per_trial} requests/trial and {budget.max_elapsed_seconds} seconds. The reviewed baseline plan remains fixed during execution. Cluster setup has separate timeouts.</p>
      <PlanView evaluation={job.preview} />
      <p>Static admission: <strong>{job.preview.admission.outcome}</strong> · {job.preview.findings.length} finding(s)</p>
      {job.load_error && <div className="error-banner" role="alert">Static analysis is available, but this chart cannot receive the CPU load: {job.load_error}</div>}
      {job.preview.findings.length > 0 && <ul>{job.preview.findings.map((finding) => <li key={finding.id}>{finding.severity}: {finding.title}</li>)}</ul>}
      {job.status === 'prepared' && (
        <>
          <label className="form-checkbox"><input type="checkbox" checked={approved} onChange={(event) => onApprovalChange(event.target.checked)} /> I approve creating a disposable kind cluster, installing this exact chart, and sending bounded CPU load to its fixture Pod.</label>
          <button className="primary-button" type="button" onClick={onStart} disabled={!approved || busy || job.preview.admission.outcome !== 'admit' || Boolean(job.load_error)}>Approve and start real load</button>
        </>
      )}
      {['queued', 'running'].includes(job.status) && <p>Run {job.status}. The page checks for results automatically; installation and CPU sampling can take several minutes.</p>}
      {job.status === 'error' && <div className="error-banner" role="alert">{job.error}</div>}
      {job.status === 'completed' && <p>Completed. The sealed evaluation is in local history; select it to inspect checks, CPU observations, findings and the cleanup result.</p>}
    </div>
  )
}
