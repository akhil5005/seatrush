// "/login" and "/signup". One component, two routes.

import React, { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ApiError } from '../api.js'
import { useSession, useToast } from '../state.jsx'

function AuthForm({ mode }) {
  const isLogin = mode === 'login'
  const { login, signup } = useSession()
  const { pushApiError } = useToast()
  const navigate = useNavigate()

  const [email, setEmail] = useState(isLogin ? 'demo@seatrush.dev' : '')
  const [password, setPassword] = useState(isLogin ? 'demo123' : '')
  const [busy, setBusy] = useState(false)
  const [fieldError, setFieldError] = useState(null)

  async function onSubmit(e) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setFieldError(null)
    try {
      await (isLogin ? login(email, password) : signup(email, password))
      navigate('/')
    } catch (err) {
      if (err instanceof ApiError) {
        // Shown inline as well as in a toast: a wrong password belongs next to
        // the field, and a 429 from the edge belongs in the toast.
        setFieldError(err.message)
        pushApiError(err)
      } else {
        setFieldError('Network error. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="authwrap">
      <form className="panel authform" onSubmit={onSubmit}>
        <h1>{isLogin ? 'Log in' : 'Create an account'}</h1>

        <label htmlFor="email">Email</label>
        <input
          id="email" name="email" type="email" autoComplete="email" required
          value={email} onChange={(e) => setEmail(e.target.value)}
        />

        <label htmlFor="password">Password</label>
        <input
          id="password" name="password" type="password" required minLength={6}
          autoComplete={isLogin ? 'current-password' : 'new-password'}
          value={password} onChange={(e) => setPassword(e.target.value)}
        />

        {fieldError && <p className="fielderror" role="alert">{fieldError}</p>}

        <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
          {busy ? 'Working…' : isLogin ? 'Log in' : 'Sign up'}
        </button>

        <p className="muted small">
          {isLogin ? (
            <>No account? <Link to="/signup">Sign up</Link></>
          ) : (
            <>Already registered? <Link to="/login">Log in</Link></>
          )}
        </p>

        {isLogin && (
          <div className="devcreds">
            <p className="muted small"><strong>Demo accounts</strong> (dev only)</p>
            <ul className="muted small">
              <li><code>demo@seatrush.dev</code> / <code>demo123</code></li>
              <li><code>admin@seatrush.dev</code> / <code>admin123</code> — admin</li>
            </ul>
            <p className="muted small">
              Login is rate limited by Nginx to ~5 attempts per minute per IP,
              so repeated wrong passwords will start returning 429.
            </p>
          </div>
        )}
      </form>
    </div>
  )
}

export function Login() { return <AuthForm mode="login" /> }
export function Signup() { return <AuthForm mode="signup" /> }
