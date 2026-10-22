// App entry point: providers, then the router.

import React from 'react'
import { createRoot } from 'react-dom/client'
import {
  BrowserRouter, Navigate, Route, Routes, useLocation,
} from 'react-router-dom'

import { Layout } from './components.jsx'
import { ClockProvider, SessionProvider, ToastProvider, useSession } from './state.jsx'
import Admin from './pages/Admin.jsx'
import Checkout from './pages/Checkout.jsx'
import EventDetail from './pages/EventDetail.jsx'
import Events from './pages/Events.jsx'
import Ops from './pages/Ops.jsx'
import Tickets from './pages/Tickets.jsx'
import { Login, Signup } from './pages/Auth.jsx'
import './styles.css'

/**
 * Client-side guard for the admin pages.
 *
 * This is UX, NOT security. It stops a non-admin from loading a page full of
 * requests that would all 403. Every admin endpoint independently re-checks
 * the `role` claim in the JWT (backend/app/auth.py:current_admin), because
 * anything enforced only in the browser is not enforced at all -- a user can
 * edit the bundle or call the API directly with curl.
 */
function RequireAdmin({ children }) {
  const { user, isAdmin } = useSession()
  const location = useLocation()

  if (!user) return <Navigate to="/login" replace state={{ from: location }} />
  if (!isAdmin) {
    return (
      <div className="notice" role="alert">
        This page is for admins. Log in as <code>admin@seatrush.dev</code>.
      </div>
    )
  }
  return children
}

function NotFound() {
  return (
    <div className="empty">
      <h1>Page not found</h1>
      <p>
        That URL does not exist. Nginx served the SPA shell for it
        (<code>try_files $uri $uri/ /index.html</code>) and React Router had no
        matching route.
      </p>
      <p><a className="btn btn-primary" href="/">Back to events</a></p>
    </div>
  )
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    {/* BrowserRouter (real URLs, no #hash) is only usable because Nginx has an
        SPA fallback -- otherwise a refresh on /checkout/<uuid> would 404.

        The `future` flags opt in early to two React Router v7 behaviours.
        Without them v6 prints two deprecation warnings to the console on every
        mount, which is noise I do not want on screen during a demo. Opting in
        now also means the v7 upgrade is a version bump rather than a
        behaviour change. */}
    <BrowserRouter
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <SessionProvider>
        <ClockProvider>
          <ToastProvider>
            <Layout>
              <Routes>
                <Route path="/" element={<Events />} />
                <Route path="/events/:id" element={<EventDetail />} />
                <Route path="/checkout/:holdId" element={<Checkout />} />
                <Route path="/tickets" element={<Tickets />} />
                <Route path="/login" element={<Login />} />
                <Route path="/signup" element={<Signup />} />
                <Route path="/admin" element={<RequireAdmin><Admin /></RequireAdmin>} />
                <Route path="/ops" element={<RequireAdmin><Ops /></RequireAdmin>} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Layout>
          </ToastProvider>
        </ClockProvider>
      </SessionProvider>
    </BrowserRouter>
  </React.StrictMode>,
)
