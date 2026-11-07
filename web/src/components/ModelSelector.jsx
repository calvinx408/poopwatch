import { useState } from 'react'
import { getSettings, putSettings } from '../api'

const MODEL_LABELS = {
  'claude-opus-5': 'Claude Opus (best accuracy)',
  'claude-haiku-3-5': 'Claude Haiku (fast)',
  'ollama': 'Ollama (local/free)',
}

export default function ModelSelector({ currentModel, availableModels, onChanged }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  if (!availableModels || availableModels.length === 0) {
    return null
  }

  async function handleChange(model) {
    if (model === currentModel) return

    setLoading(true)
    setError(null)
    try {
      const settings = await getSettings()
      const updated = { ...settings }
      updated.models = { ...updated.models, verdict_model: model }
      await putSettings(updated)
      onChanged?.(model)
    } catch (err) {
      setError(err.message || 'Failed to update model')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="model-selector">
      <div className="model-label">Verdict model:</div>
      <div className="model-options">
        {(availableModels || []).map((model) => (
          <label key={model} className="model-radio">
            <input
              type="radio"
              name="verdict-model"
              value={model}
              checked={currentModel === model}
              onChange={() => handleChange(model)}
              disabled={loading}
            />
            <span className="model-name">{MODEL_LABELS[model] || model}</span>
          </label>
        ))}
      </div>
      {error && <p className="model-error">{error}</p>}
    </div>
  )
}
