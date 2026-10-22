// "/admin" -- create an event, watch its stats, reset it for a demo.
// Admin only: the route is guarded in main.jsx and the API re-checks the
// JWT's `role` claim, because a hidden button is not a permission.

import React, { useCallback, useEffect, useState } from 'react'

import { ApiError, request } from '../api.js'
import { ErrorBox, Price, Spinner } from '../components.jsx'
import { formatDateTime } from '../hooks.js'
import { useAction, useSession, useToast } from '../state.jsx'

/** `datetime-local` gives "2026-10-22T18:00" with no zone. The API rejects a
 *  naive datetime on purpose, so attach the browser's real offset here. */
function localInputToIso(value) {
  if (!value) return null
  return new Date(value).toISOString()
}

function defaultLocal(offsetMinutes) {
  const d = new Date(Date.now() + offsetMinutes * 60_000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
         `T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export default function Admin() {
  const { token } = useSession()
  const { push, pushApiError } = useToast()

  const [events, setEvents] = useState(null)
  const [error, setError] = useState(null)
  const [statsFor, setStatsFor] = useState(null)
  const [stats, setStats] = useState(null)

  const loadEvents = useCallback(async () => {
    try {
      const { data } = await request('/api/events')
      setEvents(data.events)
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [])

  useEffect(() => { loadEvents() }, [loadEvents])

  const loadStats = useCallback(async (eventId) => {
    setStatsFor(eventId)
    setStats(null)
    try {
      const { data } = await request(`/api/admin/events/${eventId}/stats`, { token })
      setStats(data)
    } catch (err) {
      pushApiError(err)
    }
  }, [token, pushApiError])

  // Refresh the open stats panel every 2s so the flash-sale load test can be
  // watched live from this page.
  useEffect(() => {
    if (!statsFor) return undefined
    const id = setInterval(() => {
      request(`/api/admin/events/${statsFor}/stats`, { token })
        .then(({ data }) => setStats(data))
        .catch(() => { /* a transient 429 here is not worth a toast */ })
    }, 2000)
    return () => clearInterval(id)
  }, [statsFor, token])

  // ---- create -------------------------------------------------------------
  const [form, setForm] = useState({
    title: '', venue: '',
    starts_at: defaultLocal(60 * 24 * 14),   // in 2 weeks
    sale_opens_at: defaultLocal(2),          // in 2 minutes -> countdown demo
    rows: 6, cols: 10, price: 1499,
  })
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const [create, creating] = useAction(async () => {
    try {
      const { data } = await request('/api/admin/events', {
        method: 'POST', token,
        body: {
          title: form.title.trim(),
          venue: form.venue.trim(),
          starts_at: localInputToIso(form.starts_at),
          sale_opens_at: localInputToIso(form.sale_opens_at),
          rows: Number(form.rows),
          cols: Number(form.cols),
          // The API stores money in integer cents -- never floats.
          price_cents: Math.round(Number(form.price) * 100),
        },
      })
      push(`Created event ${data.id} with ${data.seats_created} seats.`, 'info')
      setForm((f) => ({ ...f, title: '', venue: '' }))
      await loadEvents()
      return data
    } catch (err) {
      if (err instanceof ApiError) { pushApiError(err); return undefined }
      throw err
    }
  })

  const [reset, resetting] = useAction(async (eventId) => {
    try {
      const { data } = await request(`/api/admin/events/${eventId}/reset`, {
        method: 'POST', token,
      })
      push(`Reset event ${eventId}: ${data.seats_freed} seats freed, ` +
           `${data.bookings_deleted} bookings and ${data.holds_deleted} holds deleted.`,
           'info', 7000)
      await loadEvents()
      if (statsFor === eventId) await loadStats(eventId)
      return data
    } catch (err) {
      if (err instanceof ApiError) { pushApiError(err); return undefined }
      throw err
    }
  })

  return (
    <>
      <div className="pagehead"><h1>Admin</h1></div>

      <section className="panel">
        <h2>Create an event</h2>
        <p className="muted small">
          Seats are generated in the same transaction as the event, so a
          half-built venue can never be visible to users.
        </p>

        <div className="formgrid">
          <div className="field">
            <label htmlFor="title">Title</label>
            <input id="title" value={form.title} onChange={set('title')}
                   placeholder="Midnight Static — Live" required />
          </div>
          <div className="field">
            <label htmlFor="venue">Venue</label>
            <input id="venue" value={form.venue} onChange={set('venue')}
                   placeholder="Phoenix Arena, Bengaluru" required />
          </div>
          <div className="field">
            <label htmlFor="starts">Event starts</label>
            <input id="starts" type="datetime-local"
                   value={form.starts_at} onChange={set('starts_at')} />
          </div>
          <div className="field">
            <label htmlFor="opens">Sale opens</label>
            <input id="opens" type="datetime-local"
                   value={form.sale_opens_at} onChange={set('sale_opens_at')} />
          </div>
          <div className="field field-sm">
            <label htmlFor="rows">Rows (1–10)</label>
            <input id="rows" type="number" min="1" max="10"
                   value={form.rows} onChange={set('rows')} />
          </div>
          <div className="field field-sm">
            <label htmlFor="cols">Seats per row (1–20)</label>
            <input id="cols" type="number" min="1" max="20"
                   value={form.cols} onChange={set('cols')} />
          </div>
          <div className="field field-sm">
            <label htmlFor="price">Price per seat (₹)</label>
            <input id="price" type="number" min="0" step="0.01"
                   value={form.price} onChange={set('price')} />
          </div>
        </div>

        <button type="button" className="btn btn-primary"
                disabled={creating || !form.title.trim() || !form.venue.trim()}
                onClick={() => create()}>
          {creating ? 'Creating…' : `Create event (${form.rows * form.cols} seats)`}
        </button>
      </section>

      <section>
        <h2>Events</h2>
        {error && <ErrorBox error={error} onRetry={loadEvents} />}
        {!events && !error && <Spinner label="Loading events" />}

        <table className="table">
          <thead>
            <tr>
              <th>ID</th><th>Title</th><th>Sale opens</th>
              <th className="num">Seats left</th><th className="num">Price</th><th />
            </tr>
          </thead>
          <tbody>
            {(events ?? []).map((ev) => (
              <tr key={ev.id}>
                <td><code>{ev.id}</code></td>
                <td>
                  {ev.title}
                  <br /><span className="muted small">{ev.venue}</span>
                </td>
                <td>
                  {formatDateTime(ev.sale_opens_at)}
                  <br />
                  <span className={`pill ${ev.sale_open ? 'pill-live' : 'pill-wait'}`}>
                    {ev.sale_open ? 'on sale' : 'not open'}
                  </span>
                </td>
                <td className="num">{ev.seats_left} / {ev.total_seats}</td>
                <td className="num"><Price cents={ev.price_cents} /></td>
                <td className="row-tight">
                  <button type="button" className="btn btn-ghost btn-sm"
                          onClick={() => loadStats(ev.id)}>
                    Stats
                  </button>
                  <button type="button" className="btn btn-danger btn-sm"
                          disabled={resetting}
                          onClick={() => reset(ev.id)}>
                    Reset
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">
          <strong>Reset</strong> deletes every hold and booking for that event
          and frees all its seats. It exists so the flash-sale load test can be
          run more than once during a demo — it is destructive and admin-only.
        </p>
      </section>

      {statsFor && (
        <section className="panel">
          <h2>Event {statsFor} — live stats</h2>
          {!stats ? <Spinner /> : (
            <>
              <div className="statgrid">
                <Stat label="Total seats" value={stats.total_seats} />
                <Stat label="Available" value={stats.available} />
                <Stat label="Held (live)" value={stats.held} />
                <Stat label="Booked" value={stats.booked} />
                <Stat label="Bookings" value={stats.bookings} />
                <Stat label="Expired holds" value={stats.expired_holds}
                      hint="Stale rows left by lazy expiry. Harmless — no sweeper runs." />
              </div>

              <div className={`integrity ${stats.integrity_ok ? 'integrity-ok' : 'integrity-bad'}`}
                   role="status">
                <strong>
                  {stats.integrity_ok ? '✓ Integrity OK' : '✕ INTEGRITY FAILURE'}
                </strong>
                <span>
                  Seats appearing in more than one booking:{' '}
                  <code>{stats.seats_in_two_bookings}</code> (must always be 0)
                  {' · '}
                  booked seat rows <code>{stats.booked_seat_rows}</code> ≤ total{' '}
                  <code>{stats.total_seats}</code>
                </span>
                <span className="muted small">
                  Computed straight from <code>booking_seats</code>, independent
                  of any application logic. <code>UNIQUE (seat_id)</code> means
                  the database cannot even store a non-zero answer.
                </span>
              </div>
            </>
          )}
        </section>
      )}
    </>
  )
}

function Stat({ label, value, hint }) {
  return (
    <div className="stat" title={hint || ''}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}
