import { Badge } from './Badge'
import { formatDate } from '../model'
import type { EvaluationSummary } from '../types'

type Props = {
  items: EvaluationSummary[]
  onSelect: (id: string) => void
}

export function EvaluationList({ items, onSelect }: Props) {
  if (items.length === 0) {
    return (
      <div className="empty-state">
        <span className="empty-state__mark">◇</span>
        <h3>No evaluations in this view</h3>
        <p>Try another search or filter, or import a bundle with the CLI.</p>
      </div>
    )
  }

  return (
    <div className="table-scroll">
      <table className="evaluations-table">
        <thead>
          <tr>
            <th>Product / chart</th>
            <th>Generated</th>
            <th>Profile</th>
            <th>Admission</th>
            <th>Findings</th>
            <th><span className="sr-only">Open evaluation</span></th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.evaluation_id}>
              <td>
                <div className="product-name">{item.chart}</div>
                <div className="secondary mono">{item.evaluation_id.slice(0, 8)} · {item.requested_version ?? 'unversioned'}</div>
              </td>
              <td className="muted">{formatDate(item.generated_at)}</td>
              <td>{item.profile_name}</td>
              <td><Badge value={item.admission_outcome} /></td>
              <td>
                <div className="finding-counts">
                  <span className={item.blocker_count ? 'count count--bad' : 'count'}>{item.blocker_count} blockers</span>
                  <span className={item.warning_count ? 'count count--warn' : 'count'}>{item.warning_count} warnings</span>
                  {item.bundle_status === 'missing' && <Badge value="missing" />}
                </div>
              </td>
              <td>
                <button
                  className="row-action"
                  type="button"
                  disabled={item.bundle_status === 'missing'}
                  onClick={() => onSelect(item.evaluation_id)}
                  aria-label={`View evaluation ${item.evaluation_id}`}
                >
                  View <span aria-hidden="true">↗</span>
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
