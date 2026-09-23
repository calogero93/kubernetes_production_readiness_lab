import { useEffect, useMemo, useState } from 'react'

import { getEvaluation, listEvaluations, syncEvaluations } from './api'
import { EvaluationDetail } from './components/EvaluationDetail'
import { EvaluationList } from './components/EvaluationList'
import { filterEvaluations, summarize, type Filter } from './model'
import type { Evaluation, EvaluationSummary } from './types'

const filters: { value: Filter; label: string }[] = [
  { value: 'all', label: 'All runs' },
  { value: 'blockers', label: 'Blockers' },
  { value: 'warnings', label: 'Warnings' },
  { value: 'missing', label: 'Missing bundles' },
]

export default function App() {
  const [items, setItems] = useState<EvaluationSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Evaluation | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  useEffect(() => {
    let active = true
    listEvaluations()
      .then((result) => { if (active) setItems(result) })
      .catch((cause: unknown) => { if (active) setError(String(cause)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!selectedId) return
    let active = true
    getEvaluation(selectedId)
      .then((result) => { if (active) setDetail(result) })
      .catch((cause: unknown) => { if (active) setDetailError(String(cause)) })
      .finally(() => { if (active) setDetailLoading(false) })
    return () => { active = false }
  }, [selectedId])

  useEffect(() => {
    if (!selectedId) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedId(null)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectedId])

  const stats = useMemo(() => summarize(items), [items])
  const visible = useMemo(() => filterEvaluations(items, query, filter), [items, query, filter])

  function openDetail(id: string) {
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    setSelectedId(id)
  }

  async function sync() {
    setSyncing(true)
    setError(null)
    try {
      await syncEvaluations()
      setItems(await listEvaluations())
    } catch (cause) {
      setError(String(cause))
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">K</span><span>KubeProof<small>Evidence workspace</small></span></div>
        <div className="nav-caption">Workspace</div>
        <div className="nav-item nav-item--active"><span aria-hidden="true">▦</span> Evaluations</div>
        <div className="sidebar-bottom">
          <span className="online-dot" /> Local history
          <p>Bundles are the source of truth. This view is a rebuildable index.</p>
        </div>
      </aside>

      <main className="main-content">
        <header className="page-header">
          <div>
            <span className="eyebrow">Overview / local evidence</span>
            <h1>Evaluation history</h1>
            <p>Trace every outcome back to checks, findings, and observations.</p>
          </div>
          <button className="primary-button" type="button" disabled={syncing} onClick={() => void sync()}>
            <span aria-hidden="true">↻</span> {syncing ? 'Syncing…' : 'Resync bundles'}
          </button>
        </header>

        {error && <div className="error-banner" role="alert">{error}</div>}

        <section className="stats-grid" aria-label="History summary">
          <div className="stat-card"><span>Evaluations</span><strong>{loading ? '—' : stats.total}</strong><small>Indexed runs</small></div>
          <div className="stat-card"><span>With blockers</span><strong className="text-bad">{loading ? '—' : stats.blocked}</strong><small>Require attention</small></div>
          <div className="stat-card"><span>Warnings</span><strong className="text-warn">{loading ? '—' : stats.warnings}</strong><small>Across all runs</small></div>
          <div className="stat-card"><span>Missing bundles</span><strong>{loading ? '—' : stats.missing}</strong><small>Index references only</small></div>
        </section>

        <section className="history-panel" aria-label="Evaluations">
          <div className="panel-heading">
            <div><span className="eyebrow">Evidence ledger</span><h2>All evaluations</h2></div>
            <span className="result-count">{visible.length} shown</span>
          </div>
          <div className="toolbar">
            <label className="search-box">
              <span aria-hidden="true">⌕</span>
              <span className="sr-only">Search by chart, profile, or ID</span>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search chart, profile, or run ID" />
            </label>
            <div className="filter-group" role="group" aria-label="Filter evaluations">
              {filters.map((option) => (
                <button key={option.value} type="button" className={filter === option.value ? 'filter-button filter-button--active' : 'filter-button'} onClick={() => setFilter(option.value)}>
                  {option.label}
                </button>
              ))}
            </div>
          </div>
          {loading ? <div className="state-message">Loading evaluations…</div> : <EvaluationList items={visible} onSelect={openDetail} />}
        </section>
        <footer className="page-footer">KubeProof · Local, evidence-driven qualification</footer>
      </main>

      {selectedId && (
        <EvaluationDetail
          evaluation={detail}
          loading={detailLoading}
          error={detailError}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  )
}
