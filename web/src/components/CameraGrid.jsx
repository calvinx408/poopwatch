import { useEffect, useRef, useState } from 'react'
import { cameraSnapshotUrl, startWatcher, stopWatcher } from '../api'

function CameraSnapshot({ camera }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function toggleCamera(cam) {
    setBusy(true)
    try {
      if (cam.state === 'stopped') await startWatcher(cam.slug)
      else await stopWatcher(cam.slug)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }
  const [timestamp, setTimestamp] = useState(Date.now())
  const [loading, setLoading] = useState(true)
  const loadedRef = useRef(false)

  useEffect(() => {
    setLoading(true)
    const timer = setTimeout(() => {
      // Only report a timeout if no image has arrived yet.
      if (!loadedRef.current) {
        setError('Timed out waiting for the camera')
        setLoading(false)
      }
    }, 15000)
    const interval = setInterval(() => {
      // A failed frame must not stick: clearing the error on each refresh
      // remounts the image, so the next attempt happens automatically.
      setError(null)
      setTimestamp(Date.now())
    }, 8000)
    return () => {
      clearTimeout(timer)
      clearInterval(interval)
    }
  }, [])

  const url = `${cameraSnapshotUrl(camera.slug)}?t=${timestamp}`

  return (
    <div className="camera-card">
      <div className="camera-snapshot">
        {error ? (
          <div className="snapshot-error">
            <p className="muted">⚠ Cannot load snapshot</p>
            <p className="small muted" style={{ fontSize: '11px', maxWidth: '90%', textAlign: 'center' }}>
              {error.slice(0, 80)}
            </p>
          </div>
        ) : (
          <>
            {loading && <div className="snapshot-loading">Loading…</div>}
            <img
              src={url}
              alt={camera.name}
              onError={async (e) => {
                try {
                  const res = await fetch(url)
                  const text = await res.text()
                  let message = `HTTP ${res.status}`
                  try {
                    const body = JSON.parse(text)
                    if (typeof body?.detail === 'string') message = body.detail
                  } catch {
                    if (text) message = text
                  }
                  setError(String(message))
                } catch (err) {
                  setError(String(err?.message || 'Failed to load'))
                }
              }}
              onLoad={() => {
                loadedRef.current = true
                setError(null)
                setLoading(false)
              }}
              crossOrigin="anonymous"
              style={{ display: loading ? 'none' : 'block' }}
            />
          </>
        )}
      </div>
      <div className="camera-info">
        <div className="camera-title">
          <h3>{camera.name}</h3>
          <button
            className={`btn ${camera.state === 'stopped' ? 'btn-primary' : ''}`}
            onClick={() => toggleCamera(camera)}
            disabled={busy}
          >
            {camera.state === 'stopped' ? 'Enable' : 'Disable'}
          </button>
        </div>
        <p className={`camera-state camera-state-${camera.state}`}>
          <span className={`dot dot-${camera.state === 'live' ? 'ok' : camera.state === 'connecting' ? 'warn' : 'bad'}`} />
          {camera.state_detail}
        </p>
        {camera.triggers_seen > 0 && (
          <p className="small muted">{camera.triggers_seen} triggers seen</p>
        )}
      </div>
    </div>
  )
}

export default function CameraGrid({ cameras, visible }) {
  // Disabled cameras are hidden here. Re-enable them with their pill in the header.
  const active = cameras.filter((cam) => cam.state !== 'stopped')
  if (!visible || active.length === 0) return null

  return (
    <div className="camera-grid">
      {active.map((cam) => (
        <CameraSnapshot key={cam.slug} camera={cam} />
      ))}
    </div>
  )
}
