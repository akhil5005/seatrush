// "/tickets" -- my bookings. Private, so Nginx never caches this response.

import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { request } from '../api.js'
import { Empty, ErrorBox, Price, Spinner } from '../components.jsx'
import { formatDateTime } from '../hooks.js'
import { useSession } from '../state.jsx'

export default function Tickets() {
  const { token } = useSession()
  const [state, setState] = useState({ loading: true, data: null, error: null })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { data } = await request('/api/bookings', { token })
        if (!cancelled) setState({ loading: false, data, error: null })
      } catch (error) {
        if (!cancelled) setState({ loading: false, data: null, error })
      }
    })()
    return () => { cancelled = true }
  }, [token])

  if (!token) {
    return <div className="notice"><Link to="/login">Log in</Link> to see your tickets.</div>
  }
  if (state.loading) return <Spinner label="Loading your tickets" />
  if (state.error) return <ErrorBox error={state.error} />

  const bookings = state.data?.bookings ?? []

  return (
    <>
      <div className="pagehead"><h1>My tickets</h1></div>

      {bookings.length === 0 && (
        <Empty>
          No tickets yet. <Link to="/">Browse events →</Link>
        </Empty>
      )}

      <ul className="tickets">
        {bookings.map((b) => (
          <li key={b.booking_id} className="ticket">
            <div className="ticket-stub">
              <span className="muted small">Booking code</span>
              <code>{b.booking_code}</code>
            </div>
            <div className="ticket-body">
              <h2>{b.event_title}</h2>
              <dl className="meta">
                <div><dt>Venue</dt><dd>{b.venue}</dd></div>
                <div><dt>Starts</dt><dd>{formatDateTime(b.starts_at)}</dd></div>
                <div>
                  <dt>Seat{b.seat_count === 1 ? '' : 's'}</dt>
                  <dd>{b.labels.join(', ')}</dd>
                </div>
                <div><dt>Paid</dt><dd><Price cents={b.total_cents} /></dd></div>
              </dl>
              <p className="muted small">Booked {formatDateTime(b.created_at)}</p>
            </div>
          </li>
        ))}
      </ul>
    </>
  )
}
