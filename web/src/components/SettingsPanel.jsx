import { useEffect, useState } from 'react'
import { getSettings, putSettings, testAlerts, testCamera } from '../api'

const STATE_LABELS = {
  live: 'Connected',
  connecting: 'Connecting…',
  stopped: 'Stopped',
  unreachable: 'Cannot reach camera',
  no_video: 'No video',
  unconfigured: 'Needs a password',
}

const STATE_TONES = {
  live: 'ok',
  connecting: 'warn',
  stopped: 'muted',
  unreachable: 'bad',
  no_video: 'bad',
  unconfigured: 'warn',
}

const blankCamera = () => ({
  name: '',
  host: '',
  channel: 0,
  user: 'admin',
  stream: 'main',
  scheme: 'auto',
  verify_tls: false,
  password: '',
  has_password: false,
  state: 'stopped',
  _new: true,
})

// A dropdown with checkboxes: click to open, tick any number of options.
function MultiSelect({ options, value, onChange, disabled, placeholder }) {
  const [open, setOpen] = useState(false)
  const chosen = options.filter((o) => value.includes(o.value))
  const summary = chosen.length
    ? chosen.map((o) => o.label).join(', ')
    : placeholder

  return (
    <div className="multiselect">
      <button
        type="button"
        className="multiselect-button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
      >
        <span>{summary}</span>
        <span aria-hidden="true">{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div className="multiselect-menu">
          {options.map((o) => (
            <label key={o.value} className="multiselect-option">
              <input
                type="checkbox"
                checked={value.includes(o.value)}
                onChange={(e) =>
                  onChange(
                    e.target.checked
                      ? [...value, o.value]
                      : value.filter((v) => v !== o.value),
                  )
                }
              />
              <span>{o.label}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

function CameraRow({ cam, index, choices, onChange, onRemove, editable }) {
  const [test, setTest] = useState(null)
  const [testing, setTesting] = useState(false)

  const set = (key) => (e) => {
    const value =
      key === 'channel'
        ? Number(e.target.value)
        : e.target.type === 'checkbox'
          ? e.target.checked
          : e.target.value
    onChange(index, { ...cam, [key]: value })
  }

  async function runTest() {
    setTesting(true)
    setTest(null)
    try {
      setTest(await testCamera(cam))
    } catch (e) {
      setTest({ ok: false, detail: e.message, steps: [] })
    } finally {
      setTesting(false)
    }
  }

  const tone = STATE_TONES[cam.state] || 'muted'

  return (
    <div className="cam-card">
      <div className="cam-card-head">
        <span className={`pill pill-${tone}`}>
          {STATE_LABELS[cam.state] || cam.state}
        </span>
        {cam.state_detail && <span className="muted small">{cam.state_detail}</span>}
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => onRemove(index)}
          disabled={!editable}
          type="button"
        >
          Remove
        </button>
      </div>

      <div className="field-grid">
        <label>
          <span>Name</span>
          <input
            value={cam.name}
            onChange={set('name')}
            placeholder="front yard"
            disabled={!editable}
          />
        </label>
        <label>
          <span>IP address</span>
          <input
            value={cam.host}
            onChange={set('host')}
            placeholder="192.168.1.50"
            disabled={!editable}
          />
        </label>
        <label>
          <span>Channel</span>
          <input
            type="number"
            min="0"
            max="31"
            value={cam.channel}
            onChange={set('channel')}
            disabled={!editable}
          />
          <small className="muted">0 for a standalone camera; 1, 2… for NVR channels</small>
        </label>
        <label>
          <span>Username</span>
          <input value={cam.user} onChange={set('user')} disabled={!editable} />
        </label>
        <label>
          <span>Password</span>
          <input
            type="password"
            value={cam.password}
            onChange={set('password')}
            placeholder={cam.has_password ? '•••••••• (stored)' : 'camera password'}
            autoComplete="new-password"
            disabled={!editable}
          />
          <small className="muted">
            {cam.has_password
              ? `Stored in ${cam.password_source === 'keyring' ? 'the OS credential store' : cam.password_source}. Leave blank to keep it.`
              : 'Saved to the OS credential store, not the config file.'}
          </small>
        </label>
        <label>
          <span>Stream</span>
          <select value={cam.stream} onChange={set('stream')} disabled={!editable}>
            {(choices.streams || ['main', 'sub']).map((s) => (
              <option key={s} value={s}>
                {s === 'main' ? 'main — full resolution' : 'sub — low res, less disk'}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Connection</span>
          <select
            value={cam.scheme || 'auto'}
            onChange={set('scheme')}
            disabled={!editable}
          >
            <option value="auto">auto — detect http or https</option>
            <option value="http">http</option>
            <option value="https">https</option>
          </select>
          <small className="muted">
            Many Reolink models redirect port 80 to https. Auto handles that.
          </small>
        </label>
        <label className="checkbox-field">
          <span className="checkbox-row">
            <input
              type="checkbox"
              checked={!cam.verify_tls}
              onChange={(e) =>
                onChange(index, { ...cam, verify_tls: !e.target.checked })
              }
              disabled={!editable}
            />
            <span>Allow self-signed certificate</span>
          </span>
          <small className="muted">
            Cameras ship certificates that can never validate. Safe on your own
            network; only honoured for private IP addresses.
          </small>
        </label>
      </div>

      <div className="cam-card-foot">
        <button className="btn btn-sm" onClick={runTest} disabled={testing || !cam.host}>
          {testing ? 'Testing…' : 'Test connection'}
        </button>
        {test && (
          <span className={test.ok ? 'ok-text' : 'alarm'}>{test.detail}</span>
        )}
      </div>

      {test?.steps?.length > 0 && (
        <ul className="test-steps">
          {test.steps.map((s) => (
            <li key={s.name}>
              <span className={s.ok ? 'ok-text' : 'alarm'}>{s.ok ? '✓' : '✕'}</span>{' '}
              <strong>{s.name}</strong> — <span className="muted">{s.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {test?.snapshot && (
        <img className="test-snapshot" src={test.snapshot} alt="Live camera frame" />
      )}
    </div>
  )
}

export default function SettingsPanel({ onClose, onSaved }) {
  const [settings, setSettings] = useState(null)
  const [cameras, setCameras] = useState([])
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [warnings, setWarnings] = useState([])
  const [dirty, setDirty] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [alertTest, setAlertTest] = useState(null)
  const [alertTesting, setAlertTesting] = useState(false)

  // Closing with unsaved edits asks first rather than silently throwing away
  // whatever was typed -- a misplaced click used to lose the whole form.
  const attemptClose = () => {
    if (dirty) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }

  useEffect(() => {
    getSettings()
      .then((s) => {
        setSettings(s)
        setCameras(s.cameras.map((c) => ({ ...c, password: '' })))
      })
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') attemptClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // Also catch a browser tab close / reload with unsaved edits.
  useEffect(() => {
    if (!dirty) return undefined
    const warn = (e) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const editable = settings?.editable !== false

  function patch(section, key, value) {
    setSettings((s) => ({ ...s, [section]: { ...s[section], [key]: value } }))
    setSaved(false)
    setDirty(true)
  }

  function updateCameras(next) {
    setCameras(next)
    setSaved(false)
    setDirty(true)
  }

  async function runAlertTest(channel) {
    const a = settings?.alerts || {}
    setAlertTesting(true)
    setAlertTest(null)
    try {
      setAlertTest(
        await testAlerts({
          channel,
          ntfy_topic: a.ntfy_topic || '',
          ntfy_server: a.ntfy_server || 'https://ntfy.sh',
          smtp_host: a.smtp_host || '',
          smtp_port: Number(a.smtp_port) || 587,
          smtp_user: a.smtp_user || '',
          smtp_password: a.smtp_password || '',
          email_to: a.email_to || '',
        }),
      )
    } catch (e) {
      setAlertTest({ ok: false, detail: e.message, steps: [] })
    } finally {
      setAlertTesting(false)
    }
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await putSettings({
        capture: settings.capture,
        models: settings.models,
        alerts: settings.alerts,
        cameras: cameras.map((c) => ({
          name: c.name,
          host: c.host,
          channel: Number(c.channel) || 0,
          user: c.user || 'admin',
          stream: c.stream || 'main',
          scheme: c.scheme || 'auto',
          verify_tls: Boolean(c.verify_tls),
          password: c.password || '',
        })),
      })
      setSettings(res.settings)
      setCameras(res.settings.cameras.map((c) => ({ ...c, password: '' })))
      setSaved(true)
      setDirty(false)
      setWarnings(res.warnings || [])
      onSaved?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  if (!settings) {
    return (
      <div className="drawer-backdrop" onClick={onClose} role="presentation">
        <aside className="drawer drawer-wide" onClick={(e) => e.stopPropagation()}>
          {error ? <p className="banner banner-error">{error}</p> : <p className="muted">Loading…</p>}
        </aside>
      </div>
    )
  }

  const { models, capture, alerts, choices } = settings

  return (
    <div className="drawer-backdrop" onClick={attemptClose} role="presentation">
      <aside className="drawer drawer-wide" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h2>Settings{dirty ? ' •' : ''}</h2>
          <button className="btn btn-ghost" onClick={attemptClose}>
            Close
          </button>
        </div>

        {confirmDiscard && (
          <div className="banner banner-warn confirm-row">
            <span>You have unsaved changes.</span>
            <span className="btn-row">
              <button className="btn btn-sm" onClick={() => setConfirmDiscard(false)}>
                Keep editing
              </button>
              <button className="btn btn-sm btn-danger" onClick={onClose}>
                Discard
              </button>
              <button
                className="btn btn-sm btn-primary"
                onClick={async () => {
                  await save()
                  onClose()
                }}
                disabled={saving}
              >
                Save and close
              </button>
            </span>
          </div>
        )}

        {!editable && <p className="banner banner-warn">{settings.editable_detail}</p>}
        {error && <p className="banner banner-error">{error}</p>}
        {saved && !error && <p className="banner banner-info">Saved. Cameras reloaded.</p>}
        {warnings.map((w) => (
          <p className="banner banner-warn" key={w}>{w}</p>
        ))}
        {!settings.keyring_available && (
          <p className="banner banner-warn">
            No OS credential store available ({settings.keyring_detail}). Passwords
            would be written to the config file in plaintext. Install it with{' '}
            <code>pip install keyring</code>.
          </p>
        )}
        {settings.plaintext_passwords?.length > 0 && (
          <p className="banner banner-warn">
            Plaintext password in the config file for:{' '}
            {settings.plaintext_passwords.join(', ')}. Re-enter and save to move it
            into the credential store.
          </p>
        )}

        <section className="settings-section">
          <div className="settings-section-head">
            <h3>Cameras</h3>
            <button
              className="btn btn-sm"
              onClick={() => updateCameras([...cameras, blankCamera()])}
              disabled={!editable}
            >
              Add camera
            </button>
          </div>
          {cameras.length === 0 && (
            <p className="muted">
              No cameras yet. Add one, enter its IP and password, then press Test
              connection before saving.
            </p>
          )}
          {cameras.map((cam, i) => (
            <CameraRow
              key={cam.slug || `new-${i}`}
              cam={cam}
              index={i}
              choices={choices}
              editable={editable}
              onChange={(idx, next) =>
                updateCameras(cameras.map((c, j) => (j === idx ? next : c)))
              }
              onRemove={(idx) => updateCameras(cameras.filter((_, j) => j !== idx))}
            />
          ))}
        </section>

        <section className="settings-section">
          <h3>Capture</h3>
          <div className="field-grid">
            <label>
              <span>Pre-roll (seconds)</span>
              <input
                type="number"
                min="0"
                max="900"
                value={capture.pre_roll_seconds}
                onChange={(e) => patch('capture', 'pre_roll_seconds', Number(e.target.value))}
                disabled={!editable}
              />
              <small className="muted">Footage kept from before the trigger</small>
            </label>
            <label>
              <span>Post-roll (seconds)</span>
              <input
                type="number"
                min="0"
                max="900"
                value={capture.post_roll_seconds}
                onChange={(e) => patch('capture', 'post_roll_seconds', Number(e.target.value))}
                disabled={!editable}
              />
              <small className="muted">How long to watch for cleanup afterwards</small>
            </label>
            <label>
              <span>Keep footage (minutes)</span>
              <input
                type="number"
                min="1"
                value={capture.segment_retention_minutes}
                onChange={(e) =>
                  patch('capture', 'segment_retention_minutes', Number(e.target.value))
                }
                disabled={!editable}
              />
              <small className="muted">Disk use ≈ cameras × this × bitrate</small>
            </label>
          </div>
        </section>

        <section className="settings-section">
          <h3>Models</h3>
          <div className="field-grid">
            <label>
              <span>Gate (runs on every trigger)</span>
              <select
                value={models.gate_backend}
                onChange={(e) => patch('models', 'gate_backend', e.target.value)}
                disabled={!editable}
              >
                {(choices.gate_backends || []).map((g) => (
                  <option key={g} value={g}>
                    {g === 'ollama'
                      ? 'ollama — local, free'
                      : g === 'haiku'
                        ? 'haiku — ~$0.001/event'
                        : 'off — send everything to the verdict model'}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Local model</span>
              <input
                value={models.ollama_model}
                onChange={(e) => patch('models', 'ollama_model', e.target.value)}
                disabled={!editable}
              />
            </label>
            <label>
              <span>Verdict model</span>
              <input
                value={models.verdict_model}
                onChange={(e) => patch('models', 'verdict_model', e.target.value)}
                disabled={!editable}
              />
              <small className="muted">Use "ollama" to run entirely free</small>
            </label>
            <label>
              <span>Concurrent analyses</span>
              <input
                type="number"
                min="1"
                max="16"
                value={models.max_concurrent_analyses}
                onChange={(e) =>
                  patch('models', 'max_concurrent_analyses', Number(e.target.value))
                }
                disabled={!editable}
              />
              <small className="muted">Across all cameras. Lower it if the PC struggles.</small>
            </label>
          </div>
        </section>

        <section className="settings-section">
          <h3>Alerts</h3>
          <div className="field-grid">
            <label>
              <span>ntfy topic</span>
              <input
                value={alerts.ntfy_topic}
                onChange={(e) => patch('alerts', 'ntfy_topic', e.target.value)}
                placeholder="something-unguessable"
                disabled={!editable}
              />
              <small className="muted">Subscribe to this topic in the ntfy phone app</small>
            </label>
            <label>
              <span>Extra alerts</span>
              <MultiSelect
                options={[
                  { value: 'good', label: 'Poop picked up' },
                  { value: 'walkby', label: 'Dog walk-by' },
                ]}
                value={[
                  ...(alerts.notify_good !== false ? ['good'] : []),
                  ...(alerts.notify_walkbys === true ? ['walkby'] : []),
                ]}
                onChange={(picked) => {
                  patch('alerts', 'notify_good', picked.includes('good'))
                  patch('alerts', 'notify_walkbys', picked.includes('walkby'))
                }}
                disabled={!editable}
                placeholder="None"
              />
              <small className="muted">Poop left behind always sends.</small>
            </label>
            <label>
              <span>Alert email to</span>
              <input
                value={alerts.email_to}
                onChange={(e) => patch('alerts', 'email_to', e.target.value)}
                disabled={!editable}
              />
            </label>
            <label>
              <span>SMTP host</span>
              <input
                value={alerts.smtp_host}
                onChange={(e) => patch('alerts', 'smtp_host', e.target.value)}
                disabled={!editable}
              />
            </label>
            <label>
              <span>SMTP user</span>
              <input
                value={alerts.smtp_user}
                onChange={(e) => patch('alerts', 'smtp_user', e.target.value)}
                disabled={!editable}
              />
            </label>
            <label>
              <span>SMTP port</span>
              <input
                type="number"
                min="1"
                max="65535"
                value={alerts.smtp_port}
                onChange={(e) => patch('alerts', 'smtp_port', Number(e.target.value))}
                disabled={!editable}
              />
              <small className="muted">587 for STARTTLS</small>
            </label>
            <label>
              <span>SMTP password</span>
              <input
                type="password"
                value={alerts.smtp_password}
                onChange={(e) => patch('alerts', 'smtp_password', e.target.value)}
                placeholder={alerts.has_smtp_password ? '•••••••• (stored)' : ''}
                autoComplete="new-password"
                disabled={!editable}
              />
              <small className="muted">
                {alerts.has_smtp_password
                  ? `Stored in ${alerts.smtp_password_source === 'keyring' ? 'the OS credential store' : alerts.smtp_password_source}. Leave blank to keep it.`
                  : 'For Gmail, use an app password, not your account password.'}
              </small>
            </label>
          </div>

          <div className="cam-card-foot">
            <button
              className="btn btn-sm"
              onClick={() => runAlertTest('email')}
              disabled={alertTesting || !editable}
            >
              {alertTesting ? 'Sending…' : 'Send test email'}
            </button>
            <button
              className="btn btn-sm"
              onClick={() => runAlertTest('ntfy')}
              disabled={alertTesting || !editable}
            >
              Send test push
            </button>
            <span className="muted small">Sends a real message now, using the values above.</span>
          </div>

          {alertTest?.steps?.length > 0 && (
            <ul className="test-steps">
              {alertTest.steps.map((s) => (
                <li key={s.name}>
                  <span className={s.ok ? 'ok-text' : 'alarm'}>{s.ok ? '✓' : '✕'}</span>{' '}
                  <strong>{s.name}</strong> — <span className="muted">{s.detail}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="settings-foot">
          <span className="muted small">
            Saved to {settings.config_file}
          </span>
          <button className="btn btn-primary" onClick={save} disabled={saving || !editable}>
            {saving ? 'Saving…' : 'Save settings'}
          </button>
        </div>
      </aside>
    </div>
  )
}
