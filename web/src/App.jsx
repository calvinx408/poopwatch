import { useCallback, useEffect, useRef, useState } from 'react'
import { getEvents, getStatus, startWatcher, stopWatcher, testTrigger } from './api'
import EventCard from './components/EventCard'
import EventDetail from './components/EventDetail'
import SettingsPanel from './components/SettingsPanel'
import StatusBar from './components/StatusBar'
import CameraGrid from './components/CameraGrid'
import LoginScreen from './components/LoginScreen'
import './styles.css'

const FILTERS = [
  { key: 'violations', label: 'Not picked up' },
  { key: 'analyzed', label: 'Analyzed' },
  { key: 'walkbys', label: 'Walk-bys' },
  { key: 'all', label: 'Everything' },
]

const POLL_MS = 5000

export default function App() {
  const [loginNeeded, setLoginNeeded] = useState(false)
  const [status, setStatus] = useState(null)
  const [events, setEvents] = useState([])
  const [eventsLoaded, setEventsLoaded] = useState(false)
  const [filter, setFilter] = useState('analyzed')
  const [cameraFilter, setCameraFilter] = useState(null)
  const [selected, setSelected] = useState(null)

  // Alert links look like /#event=42: open that event when the page loads.
  useEffect(() => {
    const match = window.location.hash.match(/event=(\d+)/)
    if (match) setSelected(Number(match[1]))
  }, [])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)
  const [showSettings, setShowSettings] = useState(false)
  const [showCameraFeed, setShowCameraFeed] = useState(true)

  // Polling reads the latest filters without re-creating the interval.
  const filters = useRef({ filter, cameraFilter })
  filters.current = { filter, cameraFilter }

  const refresh = useCallback(async () => {
    const { filter: f, cameraFilter: cam } = filters.current
    // Events and status load independently, so a slow status check never
    // holds up the event list.
    getEvents({
      onlyViolations: f === 'violations',
      includeRejected: f === 'all' || f === 'walkbys',
      walkbys: f === 'walkbys',
      camera: cam,
    })
      .then((e) => {
        setEvents(e.events)
        setEventsLoaded(true)
        setError(null)
      })
      .catch((err) => setError(err.message))
    getStatus()
      .then((s) => setStatus(s))
      .catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh, filter, cameraFilter])

  useEffect(() => {
    if (!toast) return undefined
    const id = setTimeout(() => setToast(null), 6000)
    return () => clearTimeout(id)
  }, [toast])

  async function act(fn, message) {
    setBusy(true)
    try {
      await fn()
      if (message) setToast(message)
      await refresh()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const running = status?.watcher?.running
  const cameras = status?.cameras || []
  const multiCamera = cameras.length > 1

  // Any request rejected for lack of a login brings up the login form.
  useEffect(() => {
    const onNeed = () => setLoginNeeded(true)
    window.addEventListener('poopwatch-login-required', onNeed)
    return () => window.removeEventListener('poopwatch-login-required', onNeed)
  }, [])

  if (loginNeeded) {
    return (
      <LoginScreen
        onLoggedIn={() => {
          setLoginNeeded(false)
          refresh()
        }}
      />
    )
  }

  return (
    <div className="app">
      <StatusBar
        status={status}
        busy={busy}
        showCameraFeed={showCameraFeed}
        onToggleCameraFeed={() => setShowCameraFeed(!showCameraFeed)}
        onToggleWatcher={() =>
          act(
            () => (running ? stopWatcher() : startWatcher()),
            running ? 'All watchers stopped' : 'All watchers started',
          )
        }
        onToggleCamera={(cam) =>
          act(
            () => (cam.healthy ? stopWatcher(cam.slug) : startWatcher(cam.slug)),
            `${cam.name} ${cam.healthy ? 'stopped' : 'started'}`,
          )
        }
        onOpenSettings={() => setShowSettings(true)}
        onTestTrigger={() =>
          act(
            () => testTrigger(cameraFilter),
            `Capturing ${status?.config?.post_roll ?? 120}s of footage, then analyzing…`,
          )
        }
      />

      {status && (
        <CameraGrid
          cameras={status?.cameras || []}
          visible={showCameraFeed}
        />
      )}

      {error && <p className="banner banner-error">{error}</p>}
      {toast && <p className="banner banner-info">{toast}</p>}

      {status?.demo_events > 0 && (
        <p className="banner banner-warn">
          <strong>Demo data:</strong> {status.demo_events} of the events below are
          fake samples, not footage from your cameras. Remove them with{' '}
          <code>python scripts/seed_demo.py --clear</code>.
        </p>
      )}

      {status && status.config.camera_count === 0 && (
        <p className="banner banner-info">
          No cameras configured yet.{' '}
          <button className="link-btn" onClick={() => setShowSettings(true)}>
            Open Settings
          </button>{' '}
          to add one, you can test the connection before saving.
        </p>
      )}

      <nav className="filters">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`chip ${filter === f.key ? 'chip-active' : ''}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}

        {multiCamera && (
          <>
            <span className="filter-sep" />
            <button
              className={`chip ${cameraFilter === null ? 'chip-active' : ''}`}
              onClick={() => setCameraFilter(null)}
            >
              All cameras
            </button>
            {cameras.map((cam) => (
              <button
                key={cam.slug}
                className={`chip ${cameraFilter === cam.slug ? 'chip-active' : ''}`}
                onClick={() => setCameraFilter(cam.slug)}
              >
                {cam.name}
              </button>
            ))}
          </>
        )}

        <span className="muted small">{events.length} shown</span>
      </nav>

      {events.length === 0 && !eventsLoaded ? (
        <div className="empty">
          <h2>Loading events…</h2>
        </div>
      ) : events.length === 0 ? (
        <div className="empty">
          <h2>No events yet</h2>
          <p className="muted">
            {running
              ? 'The watcher is running. Events appear here when a camera spots a dog.'
              : 'Press Start to begin recording, then Test capture to check the classifier.'}
          </p>
        </div>
      ) : (
        <main className="grid">
          {events.map((event) => (
            <EventCard
              key={event.id}
              event={event}
              selected={selected === event.id}
              onSelect={setSelected}
            />
          ))}
        </main>
      )}

      {showSettings && (
        <SettingsPanel onClose={() => setShowSettings(false)} onSaved={refresh} />
      )}

      {selected !== null && (
        <EventDetail
          eventId={selected}
          onClose={() => setSelected(null)}
          onReviewed={refresh}
          onDeleted={() => {
            setSelected(null)
            refresh()
          }}
        />
      )}
    </div>
  )
}
