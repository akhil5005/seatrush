// "/ops" -- the infrastructure dashboard. This page exists to make Nginx
// VISIBLE during the demo: round robin, rate limiting, the microcache, the
// live request log and cluster health, all on one screen.

import React, { useCallback, useEffect, useState } from 'react'

import { request } from '../api.js'
import { ErrorBox, Spinner } from '../components.jsx'
import { usePoll } from '../hooks.js'
import { useSession, useToast } from '../state.jsx'

export default function Ops() {
  const { token } = useSession()

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Ops</h1>
          <p className="muted">
            Everything on this page is produced by Nginx. The app only reads it
            back.
          </p>
        </div>
      </div>

      <ClusterHealth token={token} />
      <div className="opsgrid">
        <NginxStatus />
        <Demos token={token} />
      </div>
      <RequestLog token={token} />
    </>
  )
}

// --------------------------------------------------------------------------
// Cluster health -- the ACTIVE health check open-source Nginx does not have
// --------------------------------------------------------------------------

function ClusterHealth({ token }) {
  const { data, error, loading, refresh } = usePoll(
    (signal) => request('/api/ops/cluster', { token, signal }),
    3000,
  )

  return (
    <section>
      <h2>Cluster health</h2>
      <p className="muted small">
        Open-source Nginx has only <strong>passive</strong> health checks — it
        learns a replica is down when a real request to it fails
        (<code>max_fails=2 fail_timeout=10s</code>). The{' '}
        <code>health_check</code> directive is Nginx Plus. So the app probes its
        own siblings: this card polls <code>/api/ops/cluster</code>, which fans
        out to every peer&apos;s <code>/api/health</code> concurrently with a 1s
        timeout.
      </p>

      {loading && <Spinner label="Probing replicas" />}
      {error && <ErrorBox error={error} onRetry={refresh} />}

      {data && (
        <>
          <p className="muted small">
            Reported by <code>{data.reporter}</code> · {data.up} of {data.total} up
          </p>
          <div className="healthgrid">
            {data.peers.map((p) => (
              <div key={p.target} className={`healthcard ${p.up ? 'up' : 'down'}`}>
                <span className="dot" aria-hidden="true" />
                <strong>{p.instance ?? p.target.replace('http://', '')}</strong>
                <span className="muted small">{p.up ? 'up' : 'DOWN'}</span>
                <span className="muted small">
                  {p.up ? `db ${p.database}` : (p.error ?? 'unreachable')}
                </span>
              </div>
            ))}
          </div>
          <p className="muted small">
            Try <code>docker compose stop api2</code> — this grid turns red
            within ~3s, and the site keeps working because{' '}
            <code>proxy_next_upstream</code> retries elsewhere.
          </p>
        </>
      )}
    </section>
  )
}

// --------------------------------------------------------------------------
// stub_status
// --------------------------------------------------------------------------

/** Parses Nginx's fixed-format stub_status page.
 *  (The backend has the same parser, unit-tested, in ops.py — this one keeps
 *  the panel working even if the API is busy.) */
function parseStubStatus(text) {
  const m = /Active connections:\s*(\d+)[\s\S]*?(\d+)\s+(\d+)\s+(\d+)[\s\S]*?Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)/.exec(text || '')
  if (!m) return null
  const [, active, accepts, handled, requests, reading, writing, waiting] = m.map(Number)
  return {
    active, accepts, handled, requests, reading, writing, waiting,
    dropped: accepts - handled,
  }
}

function NginxStatus() {
  const [stub, setStub] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        // Plain text, not JSON -- stub_status has its own format.
        const res = await fetch('/nginx_status')
        const text = await res.text()
        if (!cancelled) {
          setStub(parseStubStatus(text))
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err)
      }
    }
    tick()
    const id = setInterval(tick, 3000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <section className="panel">
      <h2>Nginx status</h2>
      <p className="muted small">
        From <code>stub_status</code> at <code>/nginx_status</code>.
      </p>
      {error && <p className="muted small">Unavailable.</p>}
      {!stub && !error && <Spinner />}
      {stub && (
        <>
          <div className="statgrid">
            <Stat label="Active conns" value={stub.active} />
            <Stat label="Requests" value={stub.requests.toLocaleString()} />
            <Stat label="Accepted" value={stub.accepts.toLocaleString()} />
            <Stat label="Handled" value={stub.handled.toLocaleString()} />
            <Stat label="Reading" value={stub.reading} />
            <Stat label="Writing" value={stub.writing} />
            <Stat label="Waiting" value={stub.waiting} />
            <Stat
              label="Dropped"
              value={stub.dropped}
              bad={stub.dropped > 0}
              hint="accepts - handled. Non-zero means Nginx hit a resource limit."
            />
          </div>
          <p className="muted small">
            <strong>Dropped</strong> is the number to watch: anything above 0
            means connections were refused before being served.{' '}
            <code>Waiting</code> is idle keepalive connections — a healthy
            poll-heavy app has lots of them.
          </p>
          <p className="muted small">
            In production this endpoint would be behind{' '}
            <code>allow</code>/<code>deny</code>; it is open here only for the
            demo.
          </p>
        </>
      )}
    </section>
  )
}

// --------------------------------------------------------------------------
// The two demo buttons
// --------------------------------------------------------------------------

function Demos({ token }) {
  const { push } = useToast()
  const [rr, setRr] = useState(null)
  const [spam, setSpam] = useState(null)
  const [busy, setBusy] = useState(false)

  // Round robin: 30 requests, counted by the X-Instance response header the
  // backend middleware stamps on every response.
  const sendThirty = useCallback(async () => {
    setBusy(true); setRr(null)
    const counts = {}
    try {
      const results = await Promise.all(
        Array.from({ length: 30 }, () =>
          request('/api/health').then((r) => r.instance ?? 'unknown')
                               .catch(() => 'error')),
      )
      for (const name of results) counts[name] = (counts[name] || 0) + 1
      setRr(counts)
    } finally {
      setBusy(false)
    }
  }, [])

  // Rate limiting: 60 requests as fast as the browser will go. The general
  // /api/ zone is 20r/s with burst=40 nodelay, so the first ~40-60 pass and
  // the rest come back 429 with a JSON body and Retry-After.
  const spamSixty = useCallback(async () => {
    setBusy(true); setSpam(null)
    const counts = { 200: 0, 429: 0, other: 0 }
    let retryAfter = null
    try {
      await Promise.all(
        Array.from({ length: 60 }, () =>
          request('/api/time')
            .then(() => { counts[200] += 1 })
            .catch((err) => {
              if (err.status === 429) {
                counts[429] += 1
                retryAfter = retryAfter ?? err.retryAfter
              } else counts.other += 1
            })),
      )
      setSpam({ ...counts, retryAfter })
      if (counts[429] > 0) {
        push(`${counts[429]} of 60 requests were rate limited by Nginx.`, 'warn')
      }
    } finally {
      setBusy(false)
    }
  }, [push])

  return (
    <section className="panel">
      <h2>Edge demos</h2>

      <div className="demoblock">
        <button type="button" className="btn btn-primary" disabled={busy}
                onClick={sendThirty}>
          Send 30 requests
        </button>
        <p className="muted small">
          30 × <code>GET /api/health</code>, grouped by the{' '}
          <code>X-Instance</code> header. An even split is Nginx&apos;s round
          robin working. (It only splits evenly because the upstream is in
          shared memory — <code>zone api_backend 64k</code>. Without that, each
          of the 12 workers keeps its own counter and everything lands on api1.)
        </p>
        {rr && (
          <div className="rrbars">
            {Object.entries(rr).sort().map(([name, n]) => (
              <div className="rrbar" key={name}>
                <span className="rrbar-label"><code>{name}</code></span>
                <span className="rrbar-track">
                  <span className="rrbar-fill" style={{ width: `${(n / 30) * 100}%` }} />
                </span>
                <span className="rrbar-num">{n}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <hr />

      <div className="demoblock">
        <button type="button" className="btn btn-danger" disabled={busy}
                onClick={spamSixty}>
          Spam 60 requests
        </button>
        <p className="muted small">
          60 × <code>GET /api/time</code> at once against a{' '}
          <code>20r/s burst=40 nodelay</code> limit. The 429s never reach a
          backend — Nginx answers them itself, with our JSON envelope and a{' '}
          <code>Retry-After</code> header.
        </p>
        {spam && (
          <div className="statgrid">
            <Stat label="200 OK" value={spam[200]} />
            <Stat label="429 limited" value={spam[429]} bad={spam[429] > 0} />
            {spam.other > 0 && <Stat label="other" value={spam.other} bad />}
            {spam.retryAfter != null && (
              <Stat label="Retry-After" value={`${spam.retryAfter}s`} />
            )}
          </div>
        )}
      </div>
    </section>
  )
}

// --------------------------------------------------------------------------
// Live request log
// --------------------------------------------------------------------------

function RequestLog({ token }) {
  const { data, error, loading, refresh } = usePoll(
    (signal) => request('/api/ops/logs?limit=40', { token, signal }),
    2000,
  )

  const entries = [...(data?.entries ?? [])].reverse()   // newest first

  return (
    <section>
      <h2>Live requests</h2>
      <p className="muted small">
        The last 40 lines of Nginx&apos;s JSON access log, newest first. Nginx
        writes it to a shared volume; the API containers mount that volume{' '}
        <strong>read-only</strong> and <code>ops.py</code> seeks backwards from
        the end of the file, so the cost does not grow with log size.{' '}
        <code>/assets/</code> hits are excluded by <code>access_log off</code>,
        which keeps this table about API traffic.
      </p>

      {loading && <Spinner label="Reading the access log" />}
      {error && <ErrorBox error={error} onRetry={refresh} />}
      {data && !data.available && (
        <p className="muted small">
          Log not mounted in this container ({data.path}). Running without
          Nginx in front?
        </p>
      )}

      {entries.length > 0 && (
        <div className="tablewrap">
          <table className="table table-log">
            <thead>
              <tr>
                <th>Time</th><th>Method</th><th>URI</th>
                <th className="num">Status</th><th>Upstream</th>
                <th>Cache</th><th className="num">Time</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={`${e.time}-${i}`}>
                  <td className="mono">{(e.time || '').slice(11, 19)}</td>
                  <td><code>{e.request_method}</code></td>
                  <td className="uri" title={e.request_uri}>{e.request_uri}</td>
                  <td className="num">
                    <span className={`status status-${String(e.status)[0]}xx`}>
                      {e.status}
                    </span>
                  </td>
                  <td className="mono small">
                    {/* Empty = served from cache, no backend contacted.
                        Two addresses = a retry after a failed upstream. */}
                    {e.upstream_addr
                      ? e.upstream_addr
                      : <span className="muted">— (cache)</span>}
                    {String(e.upstream_status || '').includes(',') && (
                      <span className="badge badge-retry" title={`upstream_status: ${e.upstream_status}`}>
                        retry
                      </span>
                    )}
                  </td>
                  <td>
                    {e.upstream_cache_status
                      ? <code className={`cache cache-${e.upstream_cache_status.toLowerCase()}`}>
                          {e.upstream_cache_status}
                        </code>
                      : <span className="muted">—</span>}
                  </td>
                  <td className="num mono small">{e.request_time}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function Stat({ label, value, hint, bad }) {
  return (
    <div className={`stat ${bad ? 'stat-bad' : ''}`} title={hint || ''}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}
