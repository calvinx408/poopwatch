export const STAGE_LABELS = {
  triggered: 'Triggered',
  captured: 'Captured',
  no_dog: 'No dog in frame',
  gate_rejected: 'Filtered by gate',
  gate_failed: 'Gate error',
  capture_failed: 'Capture failed',
  verdict_failed: 'Analysis error',
  verdict_refused: 'Model declined',
  verdict: 'Analyzed',
  notified: 'Alert sent',
  interrupted: 'Interrupted by restart',
}

const FILTERED_STAGES = new Set(['no_dog', 'gate_rejected'])
const ERROR_STAGES = new Set([
  'gate_failed',
  'capture_failed',
  'verdict_failed',
  'verdict_refused',
])

export function classify(event) {
  if (event.violation) {
    return { tone: 'violation', label: 'Not picked up' }
  }
  if (ERROR_STAGES.has(event.stage)) {
    return { tone: 'error', label: STAGE_LABELS[event.stage] || 'Error' }
  }
  if (event.defecation && event.cleanup) {
    return { tone: 'clean', label: 'Cleaned up' }
  }
  if (FILTERED_STAGES.has(event.stage)) {
    return { tone: 'filtered', label: STAGE_LABELS[event.stage] }
  }
  if (event.stage === 'verdict' || event.stage === 'notified') {
    return { tone: 'neutral', label: 'No defecation' }
  }
  return { tone: 'pending', label: STAGE_LABELS[event.stage] || event.stage }
}

export function formatTime(epoch) {
  if (!epoch) return '--'
  return new Date(epoch * 1000).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatRelative(epoch) {
  if (!epoch) return 'never'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epoch))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export const formatPercent = (value) =>
  value || value === 0 ? `${Math.round(value * 100)}%` : '--'

export const formatOffset = (seconds) =>
  seconds === null || seconds === undefined || seconds < 0
    ? 'not observed'
    : `t+${Math.round(seconds)}s`
