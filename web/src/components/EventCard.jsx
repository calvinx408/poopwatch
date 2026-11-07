import { evidenceUrl } from '../api'
import { classify, formatPercent, formatTime } from '../format'

const SUBJECT_ICON = {
  dog: '🐕',
  human: '🧍',
  other_animal: '🐾',
  none: '—',
}

export default function EventCard({ event, selected, onSelect }) {
  const { tone, label } = classify(event)
  const thumb = evidenceUrl(event.thumb_path)

  return (
    <button
      className={`card card-${tone} ${selected ? 'card-selected' : ''}`}
      onClick={() => onSelect(event.id)}
      type="button"
    >
      <div className="card-thumb">
        {thumb ? (
          <img src={thumb} alt={`Event ${event.id}`} loading="lazy" />
        ) : (
          <div className="card-thumb-empty">no frame</div>
        )}
        <span className={`badge badge-${tone}`}>{label}</span>
        {event.camera_name && <span className="badge badge-cam">{event.camera_name}</span>}
        {event.review === 'false_positive' && (
          <span className="badge badge-muted">dismissed</span>
        )}
      </div>

      <div className="card-body">
        <div className="card-row">
          <span className="card-subject">
            {SUBJECT_ICON[event.subject] || '—'} {event.subject || 'unknown'}
          </span>
          <span className="muted small">{formatTime(event.trigger_at)}</span>
        </div>

        <p className="card-reason">{event.reasoning || event.error || 'Awaiting analysis…'}</p>

        <div className="card-meta muted small">
          <span>#{event.id}</span>
          {event.confidence > 0 && <span>{formatPercent(event.confidence)} confident</span>}
          {event.handler_present && <span>handler present</span>}
          {event.notified && <span>alerted</span>}
        </div>
      </div>
    </button>
  )
}
