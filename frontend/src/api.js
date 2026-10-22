// One place where every HTTP call to the backend happens.
//
// All paths are RELATIVE ('/api/events'), never absolute. That works because
// Nginx serves this bundle and proxies /api from the SAME origin, so there is
// no API base URL to configure, no CORS preflight, and nothing to get wrong
// between dev and production. `npm run dev` keeps the illusion by proxying the
// same relative paths (see vite.config.js).

/** Thrown for any non-2xx response. Carries the backend's machine-readable
 *  `code` so callers switch on that, never on human text. */
export class ApiError extends Error {
  constructor(status, code, message, details = {}, retryAfter = null) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
    this.retryAfter = retryAfter
  }
}

/**
 * @param {string} path  e.g. '/api/events'
 * @param {object} opts
 *   method, body, token, idempotencyKey, signal
 * @returns {{data: any, status: number, cacheStatus: string|null, instance: string|null}}
 */
export async function request(path, opts = {}) {
  const { method = 'GET', body, token, idempotencyKey, signal } = opts

  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers['Authorization'] = `Bearer ${token}`
  // Required by the backend on POST /api/holds and /confirm. Generated once
  // per user ACTION and reused on every retry of that action -- see useAction.
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  // Debug headers Nginx adds. The ops page and the seat map surface them, which
  // is how the load balancing and the microcache become visible in the demo.
  const cacheStatus = res.headers.get('X-Cache-Status')
  const instance = res.headers.get('X-Instance')

  let data = null
  try {
    data = await res.json()
  } catch {
    // A 204, or an edge response with no body. Not an error by itself.
    data = null
  }

  if (!res.ok) {
    // Every error in this system -- from FastAPI, from Starlette's router, and
    // from Nginx's own 429/413/503 pages -- uses the same envelope:
    //   {"error": {"code": "...", "message": "..."}}
    // so this single branch handles all of them.
    const err = (data && data.error) || {
      code: 'unknown_error',
      message: `Request failed with status ${res.status}`,
    }
    const retryAfter = Number(res.headers.get('Retry-After')) || null
    throw new ApiError(res.status, err.code, err.message, err, retryAfter)
  }

  return { data, status: res.status, cacheStatus, instance }
}

/** Convenience: most callers only want the body. */
export async function get(path, token, signal) {
  return (await request(path, { token, signal })).data
}

/** A fresh idempotency key. One per user action, not one per HTTP attempt. */
export function newIdempotencyKey() {
  // crypto.randomUUID needs a secure context -- which https://localhost is.
  // The fallback keeps `npm run dev` over plain http working.
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `k-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** 149900 -> "₹1,499.00" */
export function money(cents) {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
  }).format((cents || 0) / 100)
}
