import { useEffect, useState } from 'react'

import { approveChartRun, getChartConfig, getChartRun, prepareChartRun } from '../api'
import { defaultProfile } from '../liveRequest'
import { buildHttpProbe, defaultHttpProbe, type HttpProbeDraft } from '../httpRequest'
import type { ChartJob, LiveConfig } from '../types'
import { examplePlan, parsePlan } from '../planRequest'
import { PlanView } from './PlanView'

function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const value = reader.result
      if (typeof value !== 'string' || !value.includes(',')) {
        reject(new Error('Could not read the chart archive'))
      } else {
        resolve(value.split(',', 2)[1])
      }
    }
    reader.onerror = () => reject(new Error('Could not read the chart archive'))
    reader.readAsDataURL(file)
  })
}

type Props = { onCompleted: (evaluationId: string) => void }

export function ChartRun({ onCompleted }: Props) {
  const [config, setConfig] = useState<LiveConfig | null>(null)
  const [chart, setChart] = useState<File | null>(null)
  const [valuesYaml, setValuesYaml] = useState('')
  const [httpDraft, setHttpDraft] = useState<HttpProbeDraft>(defaultHttpProbe)
  const [planJson, setPlanJson] = useState('')
  const [aiPlan, setAiPlan] = useState(false)
  const [objective, setObjective] = useState('')
  const [aiInterpret, setAiInterpret] = useState(false)
  const [planBudget, setPlanBudget] = useState({ max_tasks: 16, max_elapsed_seconds: 900, max_parallel_tasks: 1 })
  const [profileJson, setProfileJson] = useState(JSON.stringify({ ...defaultProfile, name: 'local-chart-review' }, null, 2))
  const [job, setJob] = useState<ChartJob | null>(null)
  const [operatorLabel, setOperatorLabel] = useState('')
  const [approvalReason, setApprovalReason] = useState('')
  const [approved, setApproved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const running = job?.status === 'queued' || job?.status === 'running'
  const hostApproval = job?.preview.admission.outcome === 'static_only' && Boolean(job.approval_scope_sha256)
  const cannotRun = job?.preview.admission.outcome === 'static_only' && !job.approval_scope_sha256

  function invalidateReview() {
    setJob(null)
    setApproved(false)
  }

  function changeHttp(update: Partial<HttpProbeDraft>) {
    setHttpDraft((previous) => ({ ...previous, ...update }))
    invalidateReview()
  }

  useEffect(() => {
    getChartConfig().then(setConfig).catch((cause: unknown) => setError(String(cause)))
  }, [])

  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return
    const timer = window.setInterval(() => {
      getChartRun(job.id)
        .then((updated) => {
          setJob(updated)
          if (updated.status === 'completed' && updated.evaluation_id) {
            onCompleted(updated.evaluation_id)
          }
        })
        .catch((cause: unknown) => setError(String(cause)))
    }, 2500)
    return () => window.clearInterval(timer)
  }, [job?.id, job?.status, onCompleted])

  async function preflight(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    invalidateReview()
    if (!config?.csrf_token || !chart) {
      setError('Select a .tgz chart and start the local backend on loopback.')
      return
    }
    if (chart.size > 10 * 1024 * 1024) {
      setError('Chart archive exceeds the 10 MiB limit.')
      return
    }
    setBusy(true)
    try {
      const profile = JSON.parse(profileJson) as object
      setJob(await prepareChartRun({
        chart_name: chart.name,
        chart_base64: await readBase64(chart),
        values_yaml: valuesYaml,
        profile,
        http_probe: planJson.trim() || aiPlan ? null : buildHttpProbe(httpDraft),
        test_plan: aiPlan ? null : parsePlan(planJson),
        ai_plan: aiPlan,
        objective: aiPlan ? objective : null,
        ai_interpret: aiInterpret,
        plan_budget: planBudget,
      }, config.csrf_token))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!job || !config?.csrf_token || !approved || cannotRun) return
    setBusy(true)
    setError(null)
    try {
      setJob(await approveChartRun(job.id, {
        chart_sha256: job.chart_sha256,
        rendered_manifest_sha256: job.rendered_manifest_sha256,
        plan_sha256: job.plan_sha256,
        approval_scope_sha256: hostApproval ? job.approval_scope_sha256 : null,
        operator_label: hostApproval ? operatorLabel : null,
        approval_reason: hostApproval ? approvalReason : null,
      }, config.csrf_token))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="live-workspace" aria-label="Chart evaluation">
      <div className="live-intro">
        <h2>Review and run a Helm chart</h2>
        <p>Inspect a pinned chart and company profile before installing it in disposable local kind. Host access requires a separate recorded decision. This local lab shares the Docker host kernel.</p>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {config && !config.available && <div className="error-banner" role="alert">Chart execution is available only from the backend bound to loopback.</div>}
      <form className="live-form" onSubmit={(event) => void preflight(event)}>
        <fieldset className="live-inputs" disabled={running || busy}>
          <div className="form-grid">
            <label className="form-field form-field--wide">Helm chart archive (.tgz)
              <input type="file" accept=".tgz,application/gzip" onChange={(event) => { setChart(event.target.files?.[0] ?? null); invalidateReview() }} required />
            </label>
            <label className="form-field form-field--wide">Optional values YAML
              <textarea rows={7} value={valuesYaml} onChange={(event) => { setValuesYaml(event.target.value); invalidateReview() }} spellCheck={false} />
            </label>
            <label className="form-field form-field--wide">Company requirements (profile JSON)
              <textarea rows={17} value={profileJson} onChange={(event) => { setProfileJson(event.target.value); invalidateReview() }} spellCheck={false} />
            </label>
            <label className="form-field form-field--wide">Optional composed plan (JSON)
              <textarea rows={12} value={planJson} disabled={aiPlan} onChange={(event) => { setPlanJson(event.target.value); invalidateReview() }} spellCheck={false} placeholder="Leave empty to use the default plan, or compose registered probes." />
            </label>
            <button type="button" disabled={aiPlan} onClick={() => { setPlanJson(JSON.stringify(examplePlan, null, 2)); invalidateReview() }}>Load composed HTTP example</button>
            <label className="form-checkbox form-field--wide">
              <input type="checkbox" checked={aiPlan} onChange={(event) => { setAiPlan(event.target.checked); invalidateReview() }} /> Ask AI to propose the plan for review
            </label>
            {aiPlan && <label className="form-field form-field--wide">Evaluation objective
              <textarea rows={3} required maxLength={4000} value={objective} onChange={(event) => { setObjective(event.target.value); invalidateReview() }} />
            </label>}
            {aiPlan && <>
              <label className="form-field">Maximum plan tasks
                <input type="number" min={1} max={32} required value={planBudget.max_tasks} onChange={(event) => { setPlanBudget({ ...planBudget, max_tasks: Number(event.target.value) }); invalidateReview() }} />
              </label>
              <label className="form-field">Plan execution budget (seconds)
                <input type="number" min={1} max={1800} required value={planBudget.max_elapsed_seconds} onChange={(event) => { setPlanBudget({ ...planBudget, max_elapsed_seconds: Number(event.target.value) }); invalidateReview() }} />
              </label>
              <label className="form-field">Maximum concurrent probes
                <input type="number" min={1} max={4} required value={planBudget.max_parallel_tasks} onChange={(event) => { setPlanBudget({ ...planBudget, max_parallel_tasks: Number(event.target.value) }); invalidateReview() }} />
              </label>
            </>}
            <label className="form-checkbox form-field--wide">
              <input type="checkbox" checked={aiInterpret} onChange={(event) => { setAiInterpret(event.target.checked); invalidateReview() }} /> Ask AI to explain results and suggest changes after execution
            </label>
            {(aiPlan || aiInterpret) && <p className="form-field--wide">Uses your configured model. Planning sends the profile and redacted manifest; final interpretation also sends evaluation evidence. Suggestions are saved without applying changes.</p>}
            <label className="form-checkbox form-field--wide">
              <input type="checkbox" disabled={Boolean(planJson.trim()) || aiPlan} checked={httpDraft.enabled} onChange={(event) => changeHttp({ enabled: event.target.checked })} /> Include an HTTP Service probe in the default plan
            </label>
            {httpDraft.enabled && !planJson.trim() && !aiPlan && <>
              <p className="form-field--wide">Sequential GET requests from inside Kubernetes. Select a ClusterIP Service and its Service port. This measures endpoint behavior; it does not measure throughput or traffic distribution.</p>
              <label className="form-field">Rendered Service name
                <input value={httpDraft.service} onChange={(event) => changeHttp({ service: event.target.value })} required maxLength={63} placeholder="my-api" />
              </label>
              <label className="form-field">Service port
                <input type="number" min={1} max={65535} value={httpDraft.port} onChange={(event) => changeHttp({ port: Number(event.target.value) })} required />
              </label>
              <label className="form-field">GET path
                <input value={httpDraft.path} onChange={(event) => changeHttp({ path: event.target.value })} required maxLength={256} />
              </label>
              <label className="form-field">Requests (1–50)
                <input type="number" min={1} max={50} value={httpDraft.requests} onChange={(event) => changeHttp({ requests: Number(event.target.value) })} required />
              </label>
              <label className="form-field">Expected HTTP status
                <input type="number" min={100} max={599} value={httpDraft.expected_status} onChange={(event) => changeHttp({ expected_status: Number(event.target.value) })} required />
              </label>
              <label className="form-field">Maximum failed requests
                <input type="number" min={0} max={httpDraft.requests - 1} value={httpDraft.max_failed_requests} onChange={(event) => changeHttp({ max_failed_requests: Number(event.target.value) })} required />
              </label>
              <label className="form-field">Maximum p95 (ms, optional)
                <input type="number" min={0.001} step="any" value={httpDraft.max_p95_ms} onChange={(event) => changeHttp({ max_p95_ms: event.target.value })} />
              </label>
            </>}
          </div>
        </fieldset>
        <button className="primary-button" type="submit" disabled={busy || running || !config?.available}>{busy ? 'Checking…' : 'Review chart'}</button>
      </form>

      {job && <div className="live-review" aria-live="polite">
        <h3>{job.status === 'prepared' ? 'Review before execution' : 'Run status'}</h3>
        <p><strong>{job.chart_name}</strong> · chart SHA-256 <code>{job.chart_sha256}</code></p>
        <p>Rendered manifest SHA-256 <code>{job.rendered_manifest_sha256}</code></p>
        <p>Profile <strong>{job.preview.profile_name}</strong> · admission <strong>{job.preview.admission.outcome}</strong></p>
        <p>{job.preview.admission.explanation}</p>
        <PlanView evaluation={job.preview} />
        <p>Plan SHA-256 <code>{job.plan_sha256}</code></p>
        {job.http_probe && <p>Confirmed HTTP probe: <strong>{job.http_probe.service}:{job.http_probe.port}{job.http_probe.path}</strong> · {job.http_probe.requests} GET requests · expected {job.http_probe.expected_status} · at most {job.http_probe.max_failed_requests} failures · p95 limit {job.http_probe.max_p95_ms === null ? 'unset' : `${job.http_probe.max_p95_ms} ms`}. The probe runs after installation reaches readiness.</p>}
        {job.preview.admission.matched_rule_ids.length > 0 && <p>Local safety rules: {job.preview.admission.matched_rule_ids.join(', ')}</p>}
        <p>{job.preview.findings.filter((finding) => finding.severity === 'blocker').length} profile blocker(s), {job.preview.findings.filter((finding) => finding.severity === 'warning').length} warning(s).</p>
        {job.preview.findings.length > 0 && <details open><summary>Static findings</summary><ul>{job.preview.findings.map((finding) => <li key={finding.id}>
          <strong>{finding.severity}: {finding.title}</strong>
          <p>{finding.description}</p>
          {finding.remediation && <p>Suggested action: {finding.remediation}</p>}
          {finding.limitation && <p>Limit: {finding.limitation}</p>}
        </li>)}</ul></details>}
        {cannotRun && <div className="error-banner" role="alert">This chart also matches rules that cannot be approved for local execution.</div>}
        {hostApproval && <>
          <p><strong>Host-access risk:</strong> these workloads can access host-level resources in a kind cluster. Approval covers the listed rule categories for product Pods observed during this run. Use only an intentionally selected chart on a machine dedicated to this test.</p>
          <p>Approval scope SHA-256 <code>{job.approval_scope_sha256}</code></p>
          <div className="form-grid">
            <label className="form-field">Operator label (self-declared)
              <input value={operatorLabel} onChange={(event) => setOperatorLabel(event.target.value)} maxLength={80} disabled={job.status !== 'prepared'} />
            </label>
            <label className="form-field form-field--wide">Reason for accepting host access
              <textarea value={approvalReason} onChange={(event) => setApprovalReason(event.target.value)} maxLength={500} rows={3} disabled={job.status !== 'prepared'} />
            </label>
          </div>
        </>}
        {job.status === 'prepared' && <>
          <label className="form-checkbox"><input type="checkbox" checked={approved} onChange={(event) => setApproved(event.target.checked)} /> I approve installing this reviewed chart in a disposable local kind cluster.</label>
          <button className="primary-button" type="button" onClick={() => void start()} disabled={!approved || busy || Boolean(cannotRun) || (hostApproval && (!operatorLabel.trim() || !approvalReason.trim()))}>Approve and start</button>
        </>}
        {running && <p>Run {job.status}. The page checks for results automatically.</p>}
        {job.status === 'error' && <div className="error-banner" role="alert">{job.error}</div>}
        {job.status === 'completed' && <p>The evaluation and approval record are in local history.</p>}
      </div>}
    </section>
  )
}
