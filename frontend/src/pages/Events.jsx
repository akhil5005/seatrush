// "/" -- the event list.

import React from 'react'
import { Link } from 'react-router-dom'

import { request } from '../api.js'
import { CacheBadge, Empty, ErrorBox, Price, SaleStatus, Spinner } from '../components.jsx'
import { formatDateTime, usePoll } from '../hooks.js'
import { useClock } from '../state.jsx'

export default function Events() {
  const { serverNow, synced } = useClock()

  // 5 s is plenty for a list page -- `seats_left` is the only thing that
  // moves. Nginx microcaches this endpoint for 2 s, so most of these polls are
  // served from the edge and never reach a backend.
  const { data, meta, error, loading, refresh } = usePoll(
    (signal) => request('/api/events', { signal }),
    5000,
  )

  if (loading) return <Spinner label="Loading events" />
  if (error) return <ErrorBox error={error} onRetry={refresh} />

  const events = data?.events ?? []

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Live events</h1>
          <p className="muted">
            Seats are held for 5 minutes while you pay. Nobody can take a seat
            you are holding.
          </p>
        </div>
        <CacheBadge cacheStatus={meta.cacheStatus} />
      </div>

      {events.length === 0 && <Empty>No events yet. An admin can create one.</Empty>}

      <ul className="cards">
        {events.map((ev) => {
          const msUntilOpen = synced
            ? Math.max(0, new Date(ev.sale_opens_at).getTime() - serverNow())
            : null
          const pct = ev.total_seats
            ? Math.round(((ev.total_seats - ev.seats_left) / ev.total_seats) * 100)
            : 0

          return (
            <li key={ev.id} className="card">
              <div className="card-top">
                <h2>
                  <Link to={`/events/${ev.id}`}>{ev.title}</Link>
                </h2>
                <SaleStatus
                  saleOpen={ev.sale_open}
                  msUntilOpen={msUntilOpen}
                  seatsLeft={ev.seats_left}
                />
              </div>

              <dl className="meta">
                <div><dt>Venue</dt><dd>{ev.venue}</dd></div>
                <div><dt>Starts</dt><dd>{formatDateTime(ev.starts_at)}</dd></div>
                <div><dt>From</dt><dd><Price cents={ev.price_cents} /> per seat</dd></div>
              </dl>

              <div className="capacity">
                <div className="bar" role="img"
                     aria-label={`${ev.seats_left} of ${ev.total_seats} seats left`}>
                  <span style={{ width: `${pct}%` }} />
                </div>
                <span className="muted small">
                  <strong>{ev.seats_left}</strong> of {ev.total_seats} seats left
                </span>
              </div>

              <div className="card-actions">
                <Link
                  className={`btn ${ev.sale_open && ev.seats_left > 0 ? 'btn-primary' : 'btn-ghost'}`}
                  to={`/events/${ev.id}`}
                >
                  {ev.sold_out ? 'View seat map'
                    : ev.sale_open ? 'Pick seats' : 'View details'}
                </Link>
              </div>
            </li>
          )
        })}
      </ul>
    </>
  )
}
