const JSON_HEADERS = { 'Content-Type': 'application/json' }

async function request(path, options) {
  const res = await fetch(path, options)
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || body.fix || detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const getStatus = () => request('/api/status')

export function getEvents({
  onlyViolations = false,
  includeRejected = true,
  limit = 60,
  camera = null,
} = {}) {
  const params = new URLSearchParams({
    only_violations: String(onlyViolations),
    include_rejected: String(includeRejected),
    limit: String(limit),
  })
  if (camera) params.set('camera', camera)
  return request(`/api/events?${params}`)
}

const cameraQuery = (camera) => (camera ? `?camera=${encodeURIComponent(camera)}` : '')

export const getEvent = (id) => request(`/api/events/${id}`)

export const setReview = (id, review) =>
  request(`/api/events/${id}/review`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ review }),
  })

export const startWatcher = (camera) =>
  request(`/api/watcher/start${cameraQuery(camera)}`, { method: 'POST' })
export const stopWatcher = (camera) =>
  request(`/api/watcher/stop${cameraQuery(camera)}`, { method: 'POST' })
export const testTrigger = (camera) =>
  request(`/api/test-trigger${cameraQuery(camera)}`, { method: 'POST' })

export const getSettings = () => request('/api/settings')

export const putSettings = (settings) =>
  request('/api/settings', {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify(settings),
  })

export const testCamera = (camera) =>
  request('/api/cameras/test', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({
      name: camera.name || 'test',
      host: camera.host,
      channel: Number(camera.channel) || 0,
      user: camera.user || 'admin',
      stream: camera.stream || 'main',
      scheme: camera.scheme || 'auto',
      verify_tls: Boolean(camera.verify_tls),
      password: camera.password || '',
    }),
  })

export const testAlerts = (payload) =>
  request('/api/alerts/test', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  })

export const evidenceUrl = (relPath) =>
  relPath ? `/evidence/${relPath.split('/').map(encodeURIComponent).join('/')}` : null

export const cameraSnapshotUrl = (slug) => `/api/camera/${encodeURIComponent(slug)}/snapshot`

export const putVerdictModel = (model) =>
  request('/api/settings/verdict-model', {
    method: 'PUT',
    headers: JSON_HEADERS,
    body: JSON.stringify({ verdict_model: model }),
  })
