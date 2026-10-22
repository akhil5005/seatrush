// "/checkout/:holdId" -- the hold summary, the countdown, Pay and Release.

import React, { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ApiError, money, request } from '../api.js'
import { ErrorBox, Price, Spinner } from '../components.jsx'
import { formatDuration, useCountdown } from '../hooks.js'
import { useAction, useSession, useToast } from '../state.jsx'

export default function Checkout() {
  const { holdId } = useParams()
  const navigate = useNavigate()
  const { token } = useSession()
  const { pushApiError, push } = useToast()

  const [hold, setHold] = useState(null)
  const [error, setError] = useState(null)
  const [booking, setBooking] = useState(null)

  // Loaded once, not polled: a hold's contents never change behind the user's
  // back. The only thing that moves is the clock, and the countdown below
  // handles that locally without asking the server.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { data } = await request(`/api/holds/${holdId}`, { token })
        if (!cancelled) setHold(data)
      } catch (err) {
        if (!cancelled) setError(err)
      }
    })()
    return () => { cancelled = true }
  }, [holdId, token])

  // Counts down against the SERVER clock, so it agrees with the server's own
  // expires_at check. A client-clock countdown could show 30s remaining on a
  // hold the server already considers expired.
  const remaining = useCountdown(hold?.expires_at)
  const expired = hold && (hold.expired || remaining === 0)

  const [pay, paying] = useAction(async (idempotencyKey) => {
    try {
      const { data } = await request(`/api/holds/${holdId}/confirm`, {
        method: 'POST', token, idempotencyKey,
      })
      setBooking(data)
      push(`Paid. Your booking code is ${data.booking_code}.`, 'info', 8000)
      return data
    } catch (err) {
      if (err instanceof ApiError) {
        pushApiError(err)
        // Re-read the hold so the UI reflects whatever actually happened
        // (expired, released, already confirmed).
        try {
          const { data } = await request(`/api/holds/${holdId}`, { token })
          setHold(data)
        } catch { /* keep the existing view */ }
        return undefined
      }
      throw err
    }
  })

  const [release, releasing] = useAction(async () => {
    try {
      await request(`/api/holds/${holdId}`, { method: 'DELETE', token })
      push('Seats released.', 'info')
      navigate(`/events/${hold.event_id}`)
    } catch (err) {
      if (err instanceof ApiError) { pushApiError(err); return }
      throw err
    }
  })

  if (!token) {
    return (
      <div className="notice">
        <Link to="/login">Log in</Link> to view this hold.
      </div>
    )
  }
  if (error) {
    return (
      <>
        <ErrorBox error={error} />
        <p><Link className="btn btn-ghost" to="/">← Back to events</Link></p>
      </>
    )
  }
  if (!hold) return <Spinner label="Loading your hold" />

  // ---- paid ----------------------------------------------------------------
  if (booking || hold.status === 'confirmed') {
    const code = booking?.booking_code ?? hold.booking_code
    return (
      <div className="panel panel-ok">
        <h1>You are going 🎟</h1>
        <p>Your seats are booked. Keep this code.</p>
        <p className="bookingcode"><code>{code}</code></p>
        <dl className="meta">
          <div><dt>Event</dt><dd>{hold.event_title}</dd></div>
          <div><dt>Seats</dt><dd>{(booking?.labels ?? hold.labels).join(', ')}</dd></div>
          <div><dt>Paid</dt><dd><Price cents={booking?.total_cents ?? hold.total_cents} /></dd></div>
        </dl>
        <div className="row">
          <Link className="btn btn-primary" to="/tickets">My tickets</Link>
          <Link className="btn btn-ghost" to="/">Browse more events</Link>
        </div>
      </div>
    )
  }

  // ---- expired -------------------------------------------------------------
  if (expired) {
    return (
      <div className="panel panel-warn">
        <h1>Your hold expired</h1>
        <p>
          Seats are only held for a few minutes so they do not sit idle while
          others are waiting. These seats are free again — for anyone.
        </p>
        <div className="row">
          <Link className="btn btn-primary" to={`/events/${hold.event_id}`}>
            Pick seats again
          </Link>
          <Link className="btn btn-ghost" to="/">All events</Link>
        </div>
      </div>
    )
  }

  // ---- released ------------------------------------------------------------
  if (hold.status === 'released') {
    return (
      <div className="panel panel-warn">
        <h1>These seats were released</h1>
        <div className="row">
          <Link className="btn btn-primary" to={`/events/${hold.event_id}`}>
            Pick seats again
          </Link>
        </div>
      </div>
    )
  }

  // ---- active --------------------------------------------------------------
  const urgent = remaining != null && remaining < 60_000

  return (
    <>
      <nav className="crumbs">
        <Link to={`/events/${hold.event_id}`}>← Back to seat map</Link>
      </nav>

      <div className="checkout">
        <div className="panel">
          <h1>Confirm your seats</h1>

          <div className={`countdown ${urgent ? 'countdown-urgent' : ''}`}
               role="timer" aria-live="off">
            <span className="muted small">Seats held for</span>
            <strong>{formatDuration(remaining)}</strong>
          </div>

          <dl className="meta">
            <div><dt>Event</dt><dd>{hold.event_title}</dd></div>
            <div><dt>Venue</dt><dd>{hold.venue}</dd></div>
            <div><dt>Seats</dt><dd>{hold.labels.join(', ')}</dd></div>
            <div><dt>Per seat</dt><dd><Price cents={hold.price_cents} /></dd></div>
          </dl>

          <p className="total total-big">
            Total <Price cents={hold.total_cents} />
          </p>

          <div className="row">
            <button type="button" className="btn btn-primary btn-lg"
                    disabled={paying || releasing} onClick={() => pay()}>
              {paying ? 'Processing…' : `Pay ${money(hold.total_cents)}`}
            </button>
            <button type="button" className="btn btn-ghost"
                    disabled={paying || releasing} onClick={() => release()}>
              {releasing ? 'Releasing…' : 'Release seats'}
            </button>
          </div>

          <p className="muted small">
            No real payment is taken — this is a demo. The button stands in for
            a payment provider&apos;s callback, which is exactly the kind of
            call that gets retried, and the reason this request carries an
            <code> Idempotency-Key</code>.
          </p>
        </div>
      </div>
    </>
  )
}
