// "/events/:id" -- the seat map. This is the page the microcache exists for.

import React, { useCallback, useMemo, useState } from 'react'
import { useNavigate, useParams, Link } from 'react-router-dom'

import { ApiError, request } from '../api.js'
import {
  CacheBadge, ErrorBox, Price, SaleStatus, SeatMap, Spinner,
} from '../components.jsx'
import { formatDateTime, formatDuration, usePoll } from '../hooks.js'
import { useAction, useClock, useSession, useToast } from '../state.jsx'

const MAX_SEATS = 6          // mirrors MAX_SEATS_PER_HOLD on the server
const POLL_MS = 2500         // the poll Nginx's 1 s microcache absorbs

export default function EventDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { token } = useSession()
  const { pushApiError, push } = useToast()
  const { serverNow, synced } = useClock()

  // Seat ids the user has clicked but not yet held.
  const [selected, setSelected] = useState(() => new Set())
  // Seat ids a 409 told us we lost -- highlighted briefly so the user can see
  // what happened rather than just being told "pick again".
  const [lost, setLost] = useState(() => new Set())

  const event = usePoll((signal) => request(`/api/events/${id}`, { signal }), 5000)

  // *** THE POLL ***
  // Every open tab asks for this every 2.5 s. 1,000 tabs would be ~400 req/s
  // on the backend; Nginx's 1 s microcache plus proxy_cache_lock collapses
  // that to ~1 req/s per event. The X-Cache-Status of the last poll is printed
  // below the map, which is how that is demonstrated live.
  const seats = usePoll((signal) => request(`/api/events/${id}/seats`, { signal }), POLL_MS)

  const msUntilOpen = useMemo(() => {
    if (!event.data || !synced) return null
    return Math.max(0, new Date(event.data.sale_opens_at).getTime() - serverNow())
  }, [event.data, synced, serverNow])

  const toggle = useCallback((seat) => {
    setLost(new Set())
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(seat.id)) next.delete(seat.id)
      else if (next.size >= MAX_SEATS) {
        push(`You can hold at most ${MAX_SEATS} seats at a time.`, 'warn')
        return prev
      } else next.add(seat.id)
      return next
    })
  }, [push])

  // One Idempotency-Key per "Hold seats" click, reused if the call is retried.
  const [hold, holding] = useAction(async (idempotencyKey) => {
    try {
      const { data } = await request('/api/holds', {
        method: 'POST',
        token,
        idempotencyKey,
        body: { event_id: Number(id), seat_ids: [...selected] },
      })
      navigate(`/checkout/${data.hold_id}`)
      return data
    } catch (err) {
      if (err instanceof ApiError) {
        // 409 seat_taken: highlight exactly the seats that went, and refresh
        // the map immediately rather than waiting for the next poll.
        if (err.code === 'seat_taken') {
          setLost(new Set(err.details?.conflicting_seat_ids ?? []))
          setSelected((prev) => {
            const next = new Set(prev)
            for (const sid of err.details?.conflicting_seat_ids ?? []) next.delete(sid)
            return next
          })
          seats.refresh()
        }
        // 409 hold_limit: the user already has a hold; send them to it.
        if (err.code === 'hold_limit' && err.details?.hold_id) {
          pushApiError(err)
          navigate(`/checkout/${err.details.hold_id}`)
          return undefined
        }
        pushApiError(err)
        return undefined
      }
      throw err
    }
  })

  if (event.loading || seats.loading) return <Spinner label="Loading seat map" />
  if (event.error) return <ErrorBox error={event.error} onRetry={event.refresh} />

  const ev = event.data
  const map = seats.data
  const saleOpen = ev.sale_open && msUntilOpen === 0
  const canHold = Boolean(token) && saleOpen && selected.size > 0 && !holding

  return (
    <>
      <nav className="crumbs"><Link to="/">← All events</Link></nav>

      <div className="pagehead">
        <div>
          <h1>{ev.title}</h1>
          <p className="muted">
            {ev.venue} · {formatDateTime(ev.starts_at)} · <Price cents={ev.price_cents} /> per seat
          </p>
        </div>
        <SaleStatus saleOpen={saleOpen} msUntilOpen={msUntilOpen} seatsLeft={map.seats_left} />
      </div>

      {!saleOpen && (
        <div className="notice" role="status">
          Booking opens {formatDateTime(ev.sale_opens_at)}
          {msUntilOpen != null && <> — in <strong>{formatDuration(msUntilOpen)}</strong></>}.
          {' '}The countdown uses the <em>server</em> clock, so it is the same for everyone.
        </div>
      )}

      {seats.error && <ErrorBox error={seats.error} onRetry={seats.refresh} />}

      <div className="seatlayout">
        <SeatMap
          seats={map.seats}
          rows={ev.rows}
          cols={ev.cols}
          selected={selected}
          lostSeatIds={lost}
          onToggle={toggle}
          disabled={!saleOpen || holding}
        />

        <aside className="sidecar">
          <h2>Your selection</h2>
          <p className="seatsleft">
            Seats left: <strong>{map.seats_left}</strong> of {ev.total_seats}
          </p>

          {selected.size === 0 ? (
            <p className="muted">
              {saleOpen
                ? 'Click up to 6 available seats.'
                : 'Seat selection opens when the sale starts.'}
            </p>
          ) : (
            <>
              <ul className="chips">
                {map.seats
                  .filter((s) => selected.has(s.id))
                  .map((s) => <li key={s.id} className="chip">{s.label}</li>)}
              </ul>
              <p className="total">
                Total <Price cents={ev.price_cents * selected.size} />
                <span className="muted small"> ({selected.size} × {ev.price_cents / 100})</span>
              </p>
            </>
          )}

          {!token && (
            <p className="notice small">
              <Link to="/login">Log in</Link> to hold seats.
            </p>
          )}

          <button
            type="button"
            className="btn btn-primary btn-block"
            disabled={!canHold}
            onClick={() => hold()}
          >
            {holding ? 'Holding…' : `Hold ${selected.size || ''} seat${selected.size === 1 ? '' : 's'}`}
          </button>

          <p className="muted small">
            Holding reserves the seats for 5 minutes. You pay on the next screen.
          </p>

          <hr />
          {/* Demo instrumentation: proves the poll is being served by Nginx. */}
          <p className="muted small">
            Seat map polls every {POLL_MS / 1000}s (paused when this tab is hidden).<br />
            Last poll: <CacheBadge cacheStatus={seats.meta.cacheStatus}
                                   instance={seats.meta.instance} />
          </p>
        </aside>
      </div>
    </>
  )
}
