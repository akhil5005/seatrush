// Small presentational pieces shared by the pages.

import React from 'react'
import { Link, NavLink, useNavigate } from 'react-router-dom'

import { money } from './api.js'
import { formatDuration } from './hooks.js'
import { useSession, useToast } from './state.jsx'

// --------------------------------------------------------------------------

export function Layout({ children }) {
  const { user, isAdmin, logout } = useSession()
  const navigate = useNavigate()

  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand" aria-label="SeatRush home">
          <span className="brand-mark" aria-hidden="true">▚</span>
          SeatRush
        </Link>

        <nav className="nav" aria-label="Main">
          <NavLink to="/">Events</NavLink>
          {user && <NavLink to="/tickets">My tickets</NavLink>}
          {isAdmin && <NavLink to="/admin">Admin</NavLink>}
          {isAdmin && <NavLink to="/ops">Ops</NavLink>}
        </nav>

        <div className="session">
          {user ? (
            <>
              <span className="who" title={user.email}>
                {user.email}
                {isAdmin && <span className="badge badge-admin">admin</span>}
              </span>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => { logout(); navigate('/') }}
              >
                Log out
              </button>
            </>
          ) : (
            <>
              <Link className="btn btn-ghost" to="/login">Log in</Link>
              <Link className="btn btn-primary" to="/signup">Sign up</Link>
            </>
          )}
        </div>
      </header>

      <main className="page">{children}</main>
      <Toasts />
    </>
  )
}

// --------------------------------------------------------------------------

function Toasts() {
  const { toasts } = useToast()
  return (
    // aria-live so a screen reader announces a 409 or a 429 instead of the
    // message appearing silently.
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <span aria-hidden="true" className="toast-icon">
            {t.kind === 'error' ? '✕' : t.kind === 'warn' ? '!' : 'i'}
          </span>
          {t.message}
        </div>
      ))}
    </div>
  )
}

// --------------------------------------------------------------------------

export function Spinner({ label = 'Loading' }) {
  return (
    <p className="muted" role="status">
      <span className="spinner" aria-hidden="true" /> {label}…
    </p>
  )
}

export function ErrorBox({ error, onRetry }) {
  if (!error) return null
  return (
    <div className="errorbox" role="alert">
      <strong>{error.code || 'Error'}</strong>
      <span>{error.message}</span>
      {onRetry && (
        <button type="button" className="btn btn-ghost" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

// --------------------------------------------------------------------------

/** "On sale" / "Opens in 01:42" / "Sold out", driven by the SERVER clock. */
export function SaleStatus({ saleOpen, msUntilOpen, seatsLeft }) {
  if (saleOpen && seatsLeft === 0) {
    return <span className="pill pill-gone">Sold out</span>
  }
  if (saleOpen) {
    return <span className="pill pill-live">● On sale</span>
  }
  if (msUntilOpen == null) {
    return <span className="pill pill-wait">Opens soon</span>
  }
  return (
    <span className="pill pill-wait">
      Opens in <time>{formatDuration(msUntilOpen)}</time>
    </span>
  )
}

// --------------------------------------------------------------------------

/**
 * The seat grid.
 *
 * Accessibility, deliberately:
 *   * every seat is a real <button>, so it is in the tab order and works with
 *     Enter/Space with no extra JS;
 *   * aria-label reads "Row C, seat 7, available" -- the status is in the
 *     label, not only in the colour;
 *   * each state also carries a distinct GLYPH (·  ✕  ▣  ✓), so the map is
 *     readable with colour vision deficiency or in greyscale;
 *   * taken seats use `disabled`, which both greys them and takes them out of
 *     the tab order.
 */
export function SeatMap({ seats, rows, cols, selected, onToggle, disabled, lostSeatIds }) {
  const byRow = []
  for (const seat of seats) {
    if (!byRow[seat.row_idx]) byRow[seat.row_idx] = []
    byRow[seat.row_idx][seat.col_idx] = seat
  }

  const label = (seat) =>
    `Row ${String.fromCharCode(65 + seat.row_idx)}, seat ${seat.col_idx + 1}, ` +
    (selected.has(seat.id) ? 'selected' : seat.status)

  return (
    <div className="seatmap">
      <p className="stage" aria-hidden="true">STAGE</p>

      <div className="seatgrid" role="group" aria-label="Seat map">
        {byRow.map((row, rowIdx) => (
          <div className="seatrow" key={rowIdx}>
            <span className="rowlabel" aria-hidden="true">
              {String.fromCharCode(65 + rowIdx)}
            </span>
            {Array.from({ length: cols }, (_, colIdx) => {
              const seat = row?.[colIdx]
              if (!seat) return <span className="seat seat-gap" key={colIdx} />
              const isSelected = selected.has(seat.id)
              const isLost = lostSeatIds?.has(seat.id)
              const taken = seat.status !== 'available'
              return (
                <button
                  type="button"
                  key={seat.id}
                  className={[
                    'seat',
                    `seat-${seat.status}`,
                    isSelected ? 'seat-selected' : '',
                    isLost ? 'seat-lost' : '',
                  ].join(' ')}
                  aria-label={label(seat)}
                  aria-pressed={isSelected}
                  disabled={taken || disabled}
                  onClick={() => onToggle(seat)}
                  title={`${seat.label} — ${seat.status}`}
                >
                  <span aria-hidden="true">
                    {isSelected ? '✓' : seat.status === 'booked' ? '✕'
                      : seat.status === 'held' ? '▣' : '·'}
                  </span>
                </button>
              )
            })}
          </div>
        ))}
      </div>

      <ul className="legend">
        <li><span className="seat seat-available" aria-hidden="true"><span>·</span></span> Available</li>
        <li><span className="seat seat-selected" aria-hidden="true"><span>✓</span></span> Your selection</li>
        <li><span className="seat seat-held" aria-hidden="true"><span>▣</span></span> Held by someone</li>
        <li><span className="seat seat-booked" aria-hidden="true"><span>✕</span></span> Booked</li>
      </ul>
    </div>
  )
}

// --------------------------------------------------------------------------

/** Shows the X-Cache-Status of the last poll. Pure demo instrumentation --
 *  it is how the microcache is made visible on screen. */
export function CacheBadge({ cacheStatus, instance }) {
  if (!cacheStatus && !instance) return null
  const kind = (cacheStatus || '').toLowerCase()
  return (
    <span className="cachebadge">
      {cacheStatus && (
        <>
          <code className={`cache cache-${kind}`}>X-Cache-Status: {cacheStatus}</code>
          {' '}
        </>
      )}
      {instance && <code className="cache">X-Instance: {instance}</code>}
    </span>
  )
}

export function Price({ cents }) {
  return <span className="price">{money(cents)}</span>
}
