// Shared app state: the logged-in session, the server clock, and toasts.
// Three small contexts instead of one store -- this app has no state worth a
// state library.

import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react'

import { ApiError, newIdempotencyKey, request } from './api.js'

// --------------------------------------------------------------------------
// Session
// --------------------------------------------------------------------------

const SessionContext = createContext(null)
const STORAGE_KEY = 'seatrush.session'

function readStoredSession() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function SessionProvider({ children }) {
  // Kept in React state (memory) AND mirrored to localStorage so a refresh
  // does not log you out.
  //
  // Honest trade-off: localStorage is readable by any script on the origin, so
  // an XSS bug would expose the token. The usual alternative is an
  // httpOnly+Secure+SameSite cookie, which XSS cannot read -- but then every
  // mutating request needs CSRF protection, and Nginx would refuse to cache
  // any response carrying Set-Cookie. For a JWT-based demo API with a strict
  // same-origin CSP (which blocks the injected script in the first place),
  // localStorage is the simpler correct-enough choice. Noted in the README's
  // limitations.
  const [session, setSession] = useState(readStoredSession)

  useEffect(() => {
    if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
    else localStorage.removeItem(STORAGE_KEY)
  }, [session])

  const login = useCallback(async (email, password) => {
    const { data } = await request('/api/auth/login', {
      method: 'POST',
      body: { email, password },
    })
    setSession({ token: data.token, user: data.user })
    return data.user
  }, [])

  const signup = useCallback(async (email, password) => {
    const { data } = await request('/api/auth/signup', {
      method: 'POST',
      body: { email, password },
    })
    setSession({ token: data.token, user: data.user })
    return data.user
  }, [])

  const logout = useCallback(() => setSession(null), [])

  const value = useMemo(() => ({
    token: session?.token ?? null,
    user: session?.user ?? null,
    isAdmin: session?.user?.role === 'admin',
    login, signup, logout,
  }), [session, login, signup, logout])

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession() {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('useSession must be used inside <SessionProvider>')
  return ctx
}

// --------------------------------------------------------------------------
// Server clock
// --------------------------------------------------------------------------

const ClockContext = createContext(null)

export function ClockProvider({ children }) {
  // THE POINT OF THIS WHOLE CONTEXT:
  // the sale gate is enforced with Postgres' now(), so the countdown must tick
  // against the SERVER's clock. We fetch /api/time once, store the offset
  // (server - client), and add it to Date.now() forever after.
  //
  // Without it, a user whose laptop clock is 5 minutes fast sees "On sale" and
  // then gets 403 sale_not_open on every attempt -- a bug that is invisible on
  // the developer's correctly-synced machine.
  const [offsetMs, setOffsetMs] = useState(0)
  const [synced, setSynced] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const before = Date.now()
        const { data } = await request('/api/time')
        const after = Date.now()
        // Charge half the round trip to the response travelling back, so the
        // offset is not skewed by network latency.
        const clientMid = before + (after - before) / 2
        if (!cancelled) {
          setOffsetMs(data.epoch_ms - clientMid)
          setSynced(true)
        }
      } catch {
        // Fall back to the local clock rather than breaking every page.
        if (!cancelled) setSynced(true)
      }
    })()
    return () => { cancelled = true }
  }, [])

  const value = useMemo(() => ({
    offsetMs,
    synced,
    /** Authoritative "now", in epoch ms. */
    serverNow: () => Date.now() + offsetMs,
  }), [offsetMs, synced])

  return <ClockContext.Provider value={value}>{children}</ClockContext.Provider>
}

export function useClock() {
  const ctx = useContext(ClockContext)
  if (!ctx) throw new Error('useClock must be used inside <ClockProvider>')
  return ctx
}

// --------------------------------------------------------------------------
// Toasts
// --------------------------------------------------------------------------

const ToastContext = createContext(null)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const nextId = useRef(1)

  const push = useCallback((message, kind = 'info', ttlMs = 5000) => {
    const id = nextId.current++
    setToasts((t) => [...t, { id, message, kind }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), ttlMs)
  }, [])

  /** Turn an ApiError into the right user-facing message.
   *  Switching on `code` -- never on the message text -- is why the backend
   *  and Nginx both return a stable machine-readable code. */
  const pushApiError = useCallback((err) => {
    if (!(err instanceof ApiError)) {
      push(err?.message || 'Something went wrong', 'error')
      return
    }
    switch (err.code) {
      case 'rate_limited':
        // Retry-After comes from Nginx's @rate_limited location.
        push(
          `Too many requests, slow down. Try again in ${err.retryAfter ?? 1}s.`,
          'warn',
          (err.retryAfter ?? 1) * 1000 + 2000,
        )
        break
      case 'seat_taken': {
        const lost = err.details?.conflicting_labels
        push(
          lost?.length
            ? `Seat${lost.length > 1 ? 's' : ''} ${lost.join(', ')} just went. Pick again.`
            : 'Those seats were just taken. Pick again.',
          'warn',
        )
        break
      }
      case 'sale_not_open':
        push('Booking has not opened for this event yet.', 'warn')
        break
      case 'hold_expired':
      case 'hold_released':
        push('Your hold expired. Please pick seats again.', 'warn')
        break
      case 'hold_limit':
        push('You already have seats on hold for this event.', 'warn')
        break
      case 'seat_limit':
        push(err.message, 'warn')
        break
      case 'upstream_unavailable':
        push('The booking service is briefly unavailable. Retrying is safe.', 'error')
        break
      case 'token_expired':
      case 'unauthorized':
        push('Please log in again.', 'error')
        break
      default:
        push(err.message, 'error')
    }
  }, [push])

  const value = useMemo(() => ({ toasts, push, pushApiError }), [toasts, push, pushApiError])
  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}

// --------------------------------------------------------------------------
// useAction -- one idempotency key per user action
// --------------------------------------------------------------------------

/**
 * Wraps a mutating call so that:
 *   * it cannot be fired twice concurrently (double-click guard in the UI);
 *   * every RETRY of the same action reuses the SAME Idempotency-Key, so the
 *     server recognises it as a duplicate and replays the original response.
 *
 * A NEW key is minted only after the action completes successfully or is
 * explicitly reset -- because at that point the next click is a genuinely new
 * action, not a retry of the old one.
 */
export function useAction(fn) {
  const [busy, setBusy] = useState(false)
  const keyRef = useRef(null)

  const run = useCallback(async (...args) => {
    if (busy) return undefined
    if (!keyRef.current) keyRef.current = newIdempotencyKey()
    setBusy(true)
    try {
      const result = await fn(keyRef.current, ...args)
      keyRef.current = null        // success -> the next click is a new action
      return result
    } finally {
      setBusy(false)
    }
  }, [busy, fn])

  return [run, busy]
}
