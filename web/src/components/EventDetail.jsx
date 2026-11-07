import { useEffect, useState } from 'react'
import { evidenceUrl, getEvent, setReview } from '../api'
import { classify, formatOffset, formatPercent, formatTime, STAGE_LABELS } from '../format'

const REVIEW_BUTTONS = [
  { key: 'confirmed', label: 'Correct' },
  { key: 'false_positive', label: 'False positive' },
  { key: 'handled', label: 'Handled' },
]

export default function EventDetail({ eventId, onClose, onReviewed }) {
  const [event, setEvent] = useState(null)
  const [error, setError] = useState(null)
  const [frameIndex, setFrameIndex] = useState(0)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let active = true
    setEvent(null)
    setError(null)
    setFrameIndex(0)
    getEvent(eventId)
      .then((data) => active && setEvent(data))
      .catch((e) => active && setError(e.message))
    return () => {
      active = false
    }
  }, [eventId])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function review(key) {
    setSaving(true)
    try {
      const res = await setReview(eventId, key)
      setEvent(res.event)
      onReviewed?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const frames = event?.frame_paths || []
  const verdict = event?.verdict

  return (
    <div className="drawer-backdrop" onClick={onClose} role="presentation">
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h2>Event #{eventId}</h2>
          <button className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>

        {error && <p className="banner banner-error">{error}</p>}
        {!event && !error && <p className="muted">Loading…</p>}

        {event && (
          <>
            <div className="drawer-summary">
              <span className={`badge badge-${classify(event).tone}`}>
                {classify(event).label}
              </span>
              <span className="muted">{formatTime(event.trigger_at)}</span>
              <span className="muted">{STAGE_LABELS[event.stage] || event.stage}</span>
            </div>

            {event.clip_path && (
              <video className="drawer-video" src={evidenceUrl(event.clip_path)} controls />
            )}

            {frames.length > 0 && (
              <>
                <img
                  className="drawer-frame"
                  src={evidenceUrl(frames[frameIndex])}
                  alt={`Frame ${frameIndex + 1}`}
                />
                <input
                  className="drawer-scrub"
                  type="range"
                  min={0}
                  max={frames.length - 1}
                  value={frameIndex}
                  onChange={(e) => setFrameIndex(Number(e.target.value))}
                />
                <p className="muted small center">
                  frame {frameIndex + 1} / {frames.length} · t+{frameIndex}s
                </p>
              </>
            )}

            <dl className="drawer-facts">
              <div>
                <dt>Camera</dt>
                <dd>{event.camera_name || 'unknown'}</dd>
              </div>
              <div>
                <dt>Subject</dt>
                <dd>{event.subject || 'unknown'}</dd>
              </div>
              <div>
                <dt>Defecation</dt>
                <dd>
                  {event.defecation ? 'yes' : 'no'}
                  {verdict && event.defecation && (
                    <span className="muted"> · {formatOffset(verdict.defecation_offset_s)}</span>
                  )}
                </dd>
              </div>
              <div>
                <dt>Handler present</dt>
                <dd>{event.handler_present ? 'yes' : 'no'}</dd>
              </div>
              <div>
                <dt>Picked up</dt>
                <dd className={event.defecation && !event.cleanup ? 'alarm' : ''}>
                  {event.cleanup ? 'yes' : 'no'}
                  {verdict && event.cleanup && (
                    <span className="muted"> · {formatOffset(verdict.cleanup_offset_s)}</span>
                  )}
                </dd>
              </div>
              <div>
                <dt>Confidence</dt>
                <dd>{formatPercent(event.confidence)}</dd>
              </div>
              <div>
                <dt>Alerted</dt>
                <dd>{event.notified ? 'yes' : 'no'}</dd>
              </div>
            </dl>

            {event.reasoning && (
              <section className="drawer-note">
                <h3>Reasoning</h3>
                <p>{event.reasoning}</p>
              </section>
            )}
            {event.cleanup_evidence && (
              <section className="drawer-note">
                <h3>On cleanup</h3>
                <p>{event.cleanup_evidence}</p>
              </section>
            )}
            {event.error && (
              <section className="drawer-note">
                <h3>Error</h3>
                <p className="alarm">{event.error}</p>
              </section>
            )}

            <div className="drawer-review">
              <span className="muted small">
                Review{event.review !== 'unreviewed' ? `: ${event.review}` : ''}
              </span>
              <div className="btn-row">
                {REVIEW_BUTTONS.map((b) => (
                  <button
                    key={b.key}
                    className={`btn ${event.review === b.key ? 'btn-primary' : ''}`}
                    disabled={saving}
                    onClick={() => review(b.key)}
                  >
                    {b.label}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
      </aside>
    </div>
  )
}
