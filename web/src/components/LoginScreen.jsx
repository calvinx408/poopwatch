import { useState } from 'react'
import { loginDashboard } from '../api'

// Shown when the dashboard has a password and this browser isn't signed in.
// A form works in home-screen web apps, where the browser's own prompt may not appear.
export default function LoginScreen({ onLoggedIn }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await loginDashboard(password)
      onLoggedIn()
    } catch (err) {
      setError(err.message === 'wrong password' ? 'Wrong password' : err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <h1>PoopWatch</h1>
        <p className="muted small">Enter the dashboard password to continue.</p>
        <input
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoFocus
        />
        {error && <p className="banner banner-error">{error}</p>}
        <button className="btn btn-primary" type="submit" disabled={busy || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
