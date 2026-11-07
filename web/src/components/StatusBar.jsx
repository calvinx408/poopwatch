import { formatRelative } from '../format'
import ModelSelector from './ModelSelector'
import BrandMark from './BrandMark'

function Dot({ ok, warn }) {
  const tone = ok ? 'ok' : warn ? 'warn' : 'bad'
  return <span className={`dot dot-${tone}`} />
}

const STATE_LABELS = {
  live: 'live',
  connecting: 'connecting',
  stopped: 'stopped',
  unreachable: 'unreachable',
  no_video: 'no video',
  unconfigured: 'no password',
}

function CameraPill({ cam, onToggle, busy }) {
  const title = [
    cam.state_detail,
    cam.healthy ? 'Click to stop this camera' : 'Click to start this camera',
    `${cam.host} ch${cam.channel} · ${cam.stream}`,
    cam.detect_source,
    cam.triggers_seen ? `${cam.triggers_seen} triggers` : null,
    cam.last_error,
  ]
    .filter(Boolean)
    .join('\n')

  return (
    <button
      className={`cam-pill ${cam.healthy ? 'cam-pill-on' : 'cam-pill-off'}`}
      title={title}
      onClick={() => onToggle(cam)}
      disabled={busy}
      type="button"
    >
      <Dot
        ok={cam.state === 'live'}
        warn={cam.state === 'connecting' || cam.state === 'unconfigured'}
      />
      <span className="cam-pill-name">{cam.name}</span>
      <span className="cam-pill-state">{STATE_LABELS[cam.state] || cam.state}</span>
      {cam.in_flight > 0 && <span className="cam-pill-busy">{cam.in_flight}</span>}
    </button>
  )
}

export default function StatusBar({
  status,
  busy,
  showCameraFeed,
  onToggleCameraFeed,
  onToggleWatcher,
  onToggleCamera,
  onTestTrigger,
  onOpenSettings,
}) {
  if (!status) {
    return (
      <header className="statusbar">
        <div className="brand">
          <span className="brand-mark"><BrandMark /></span>
          <div>
            <h1>PoopWatch</h1>
            <p className="muted">connecting…</p>
          </div>
        </div>
      </header>
    )
  }

  const { watcher, stats, config } = status
  const cameras = watcher.cameras || []
  const running = watcher.running
  const allHealthy = watcher.all_healthy

  return (
    <header className="statusbar">
      <div className="brand">
        <span className="brand-mark"><BrandMark /></span>
        <div>
          <h1>PoopWatch</h1>
          <p className="muted">
            {config.camera_count === 0
              ? 'No cameras configured'
              : `${watcher.live_count ?? 0}/${config.camera_count} live`}{' '}
            · {config.pre_roll}s pre / {config.post_roll}s post
          </p>
        </div>
      </div>

      <div className="health">
        {cameras.map((cam) => (
          <CameraPill key={cam.slug} cam={cam} onToggle={onToggleCamera} busy={busy} />
        ))}
      </div>

      <ModelSelector
        currentModel={config.verdict_model}
        availableModels={config.available_models || ['ollama', 'claude-haiku-3-5', 'claude-opus-5']}
        onChanged={(model) => {
          config.verdict_model = model
        }}
      />

      <div className="counters">
        <div className="counter">
          <strong className={stats.violations ? 'alarm' : ''}>{stats.violations}</strong>
          <span>not picked up</span>
        </div>
        <div className="counter">
          <strong>{stats.last_24h}</strong>
          <span>last 24h</span>
        </div>
        <div className="counter">
          <strong>{stats.gate_rejected}</strong>
          <span>filtered</span>
        </div>
      </div>

      <div className="actions">
        <span className="muted small">
          {watcher.in_flight > 0
            ? `${watcher.in_flight} analyzing`
            : running
              ? `polled ${formatRelative(Math.max(...cameras.map((c) => c.last_poll), 0))}`
              : 'idle'}
          {running && !allHealthy && ' · some cameras down'}
        </span>
        {cameras.length > 0 && (
          <button
            className={`btn ${showCameraFeed ? 'btn-active' : ''}`}
            onClick={onToggleCameraFeed}
            title="Toggle live camera feed view"
          >
            {showCameraFeed ? '👁 Hide feed' : '👁 Show feed'}
          </button>
        )}
        <button className="btn" onClick={onOpenSettings}>
          Settings
        </button>
        <button className="btn" onClick={onTestTrigger} disabled={busy || !running}>
          Test capture
        </button>
        <button
          className={`btn ${running ? 'btn-danger' : 'btn-primary'}`}
          onClick={onToggleWatcher}
          disabled={busy}
        >
          {running ? 'Stop all' : 'Start all'}
        </button>
      </div>

      {(watcher.errors || []).map((e) => (
        <p className="banner banner-warn" key={e.camera}>
          <strong>{e.camera}:</strong> {e.error}
        </p>
      ))}
      {!config.gate_ready && config.gate_detail && (
        <p className="banner banner-warn">{config.gate_detail}</p>
      )}
    </header>
  )
}
