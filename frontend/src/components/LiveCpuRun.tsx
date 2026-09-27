import { useEffect, useState } from 'react'

import { approveLiveRun, getLiveConfig, getLiveRun, prepareLiveRun } from '../api'
import { buildCpuRequest, defaultProfile } from '../liveRequest'
import type { LiveConfig, LiveJob } from '../types'
import { LiveRunReview } from './LiveRunReview'

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

export function LiveCpuRun({ onCompleted }: Props) {
  const [config, setConfig] = useState<LiveConfig | null>(null)
  const [chart, setChart] = useState<File | null>(null)
  const [profileJson, setProfileJson] = useState(JSON.stringify(defaultProfile, null, 2))
  const [targetRps, setTargetRps] = useState(5)
  const [maxP95Ms, setMaxP95Ms] = useState(500)
  const [maxFailed, setMaxFailed] = useState(0)
  const [maxCpu, setMaxCpu] = useState(500)
  const [iterations, setIterations] = useState(20_000)
  const [useAi, setUseAi] = useState(false)
  const [approved, setApproved] = useState(false)
  const [job, setJob] = useState<LiveJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const running = job?.status === 'queued' || job?.status === 'running'

  function invalidateReview() {
    setJob(null)
    setApproved(false)
  }

  useEffect(() => {
    getLiveConfig().then(setConfig).catch((cause: unknown) => setError(String(cause)))
  }, [])

  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return
    const timer = window.setInterval(() => {
      getLiveRun(job.id)
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
    setApproved(false)
    setJob(null)
    if (!config?.csrf_token || !chart) {
      setError('Select a .tgz chart and ensure the local backend is available.')
      return
    }
    if (chart.size > 10 * 1024 * 1024) {
      setError('Chart archive exceeds the 10 MiB upload limit.')
      return
    }
    setBusy(true)
    try {
      const profile = JSON.parse(profileJson) as object
      const request = buildCpuRequest({ targetRps, maxP95Ms, maxFailed, maxCpu, iterations, useAi })
      setJob(await prepareLiveRun({
        chart_name: chart.name,
        chart_base64: await readBase64(chart),
        profile,
        request,
        use_ai: useAi,
      }, config.csrf_token))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function start() {
    if (!job || !config?.csrf_token || !approved) return
    setBusy(true)
    setError(null)
    try {
      setJob(await approveLiveRun(job.id, job.chart_sha256, config.csrf_token))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="live-workspace" aria-label="Real CPU evaluation">
      <div className="live-intro">
        <h2>Real CPU evaluation</h2>
        <p>Upload the CPU fixture Helm chart, confirm the requirements, review its static admission, then explicitly approve a bounded run in disposable kind. Load targets one named Pod; it does not measure Service load balancing.</p>
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {config && !config.available && <div className="error-banner" role="alert">Live evaluation is unavailable. Start the local backend with the AI dependencies installed and bind it to loopback.</div>}
      <form className="live-form" onSubmit={(event) => void preflight(event)}>
        <fieldset className="live-inputs" disabled={running || busy}>
        <div className="form-grid">
          <label className="form-field form-field--wide">Helm chart archive (.tgz)
            <input type="file" accept=".tgz,application/gzip" onChange={(event) => { setChart(event.target.files?.[0] ?? null); invalidateReview() }} required />
          </label>
          <label className="form-field">Target requests/second
            <input type="number" min="0.1" max="200" step="0.1" value={targetRps} onChange={(event) => { setTargetRps(Number(event.target.value)); invalidateReview() }} required />
          </label>
          <label className="form-field">Maximum p95 latency (ms)
            <input type="number" min="1" value={maxP95Ms} onChange={(event) => { setMaxP95Ms(Number(event.target.value)); invalidateReview() }} required />
          </label>
          <label className="form-field">Maximum failed requests
            <input type="number" min="0" value={maxFailed} onChange={(event) => { setMaxFailed(Number(event.target.value)); invalidateReview() }} required />
          </label>
          <label className="form-field">Maximum Pod CPU (millicores)
            <input type="number" min="1" value={maxCpu} onChange={(event) => { setMaxCpu(Number(event.target.value)); invalidateReview() }} required />
          </label>
          <label className="form-field">Work iterations per request
            <input type="number" min="1" max="2000000" value={iterations} onChange={(event) => { setIterations(Number(event.target.value)); invalidateReview() }} required />
          </label>
          <label className="form-field form-field--wide">Company requirements (profile JSON)
            <textarea rows={17} value={profileJson} onChange={(event) => { setProfileJson(event.target.value); invalidateReview() }} spellCheck={false} />
          </label>
        </div>
        <label className="form-checkbox"><input type="checkbox" checked={useAi} onChange={(event) => { setUseAi(event.target.checked); invalidateReview() }} /> Use the configured AI supervisor (otherwise run one deterministic pilot trial)</label>
        </fieldset>
        <button className="primary-button" type="submit" disabled={busy || running || !config?.available}>{busy ? 'Checking…' : 'Review chart and requirements'}</button>
      </form>
      {job && <LiveRunReview job={job} approved={approved} busy={busy} onApprovalChange={setApproved} onStart={() => void start()} />}
    </section>
  )
}
