import { Badge } from './Badge'
import { formatDate, resourceLabel } from '../model'
import type { Evaluation, Finding, Observation } from '../types'

type Props = {
  evaluation: Evaluation | null
  loading: boolean
  error: string | null
  onClose: () => void
}

function FindingCard({ finding }: { finding: Finding }) {
  return (
    <article className={`finding-card finding-card--${finding.severity}`}>
      <div className="finding-card__heading">
        <Badge value={finding.severity} />
        <strong>{finding.title}</strong>
      </div>
      <p>{finding.description}</p>
      {finding.constraint && <p className="secondary">Constraint: <code>{finding.constraint}</code></p>}
      {finding.remediation && <p className="secondary">Remediation: {finding.remediation}</p>}
      {finding.limitation && <p className="secondary">Limitation: {finding.limitation}</p>}
    </article>
  )
}

function ObservationCard({ observation }: { observation: Observation }) {
  return (
    <article className="observation-card">
      <div className="observation-card__heading">
        <span className="eyebrow">{observation.source_class.replaceAll('_', ' ')}</span>
        <span className="mono secondary">{observation.id}</span>
      </div>
      <strong>{resourceLabel(observation.resource)}</strong>
      <p>{observation.summary}</p>
      {observation.provenance && (
        <p className="secondary mono">Source: {observation.provenance.source_ref}</p>
      )}
      {Object.keys(observation.data).length > 0 && (
        <details>
          <summary>Structured evidence</summary>
          <pre>{JSON.stringify(observation.data, null, 2)}</pre>
        </details>
      )}
    </article>
  )
}

export function EvaluationDetail({ evaluation, loading, error, onClose }: Props) {
  const findings = new Map(evaluation?.findings.map((item) => [item.id, item]) ?? [])
  const observations = new Map(evaluation?.observations.map((item) => [item.id, item]) ?? [])

  return (
    <div className="drawer-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <aside className="detail-drawer" role="dialog" aria-modal="true" aria-label="Evaluation detail">
        <div className="detail-drawer__top">
          <div>
            <span className="eyebrow">Evidence record</span>
            <h2>{evaluation?.input.chart ?? 'Evaluation detail'}</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close detail">×</button>
        </div>
        <div className="detail-drawer__body">
          {loading && <p className="state-message">Loading evidence…</p>}
          {error && <p className="error-message" role="alert">{error}</p>}
          {evaluation && (
            <>
              <div className="detail-meta">
                <span>{formatDate(evaluation.generated_at)}</span>
                <span>Profile: {evaluation.profile_name}</span>
                <span>Schema {evaluation.schema_version}</span>
              </div>
              <div className="identity-card">
                <span className="eyebrow">Run identity</span>
                <code>{evaluation.evaluation_id}</code>
                <p>Admission <Badge value={evaluation.admission.outcome} /></p>
                <p className="secondary">{evaluation.admission.explanation}</p>
                {evaluation.input.chart_package_sha256 && (
                  <p className="secondary mono digest">Chart SHA-256: {evaluation.input.chart_package_sha256}</p>
                )}
              </div>
              <h3 className="section-heading">Checks <span>{evaluation.checks.length}</span></h3>
              <div className="check-list">
                {evaluation.checks.map((check) => (
                  <section className="check-card" key={check.id}>
                    <div className="check-card__top">
                      <div>
                        <h4>{check.title}</h4>
                        <p className="secondary mono">{check.id} · experiment v{check.experiment_version}</p>
                      </div>
                      <Badge value={check.assessment} />
                    </div>
                    <p className="secondary">Execution: {check.execution_status.replaceAll('_', ' ')}</p>
                    {check.explanation && <p>{check.explanation}</p>}
                    {check.finding_ids.map((id) => findings.get(id)).filter((item): item is Finding => !!item).map((item) => (
                      <FindingCard key={item.id} finding={item} />
                    ))}
                    {check.observation_ids.length > 0 && (
                      <details className="evidence-group">
                        <summary>Observations <span>{check.observation_ids.length}</span></summary>
                        <div className="observation-list">
                          {check.observation_ids.map((id) => observations.get(id)).filter((item): item is Observation => !!item).map((item) => (
                            <ObservationCard key={item.id} observation={item} />
                          ))}
                        </div>
                      </details>
                    )}
                  </section>
                ))}
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  )
}
