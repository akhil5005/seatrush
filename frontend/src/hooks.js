// Two small hooks used by more than one page.

import { useCallback, useEffect, useRef, useState } from 'react'

import { useClock } from './state.jsx'

/**
 * Poll a fetcher on an interval, and STOP while the tab is hidden.
 *
 * The visibility check is not a micro-optimisation -- it is the difference
 * between "1,000 open tabs" and "1,000 open tabs of which 700 are in a
 * background window still hammering the API". document.visibilityState is the
 * cheapest possible way to not be that app. (Nginx's microcache would absorb
 * it anyway, but not making the request at all is strictly better.)
 *
 * @param {() => Promise<{data:any, cacheStatus?:string|null, instance?:string|null}>} fetcher
 * @param {number} intervalMs
 * @param {boolean} enabled
 */
export function usePoll(fetcher, intervalMs, enabled = true) {
  const [data, setData] = useState(null)
  const [meta, setMeta] = useState({ cacheStatus: null, instance: null, at: null })
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  // Hold the fetcher in a ref so a caller can pass an inline arrow function
  // without restarting the interval on every render.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const tick = useCallback(async (signal) => {
    try {
      const res = await fetcherRef.current(signal)
      if (signal?.aborted) return
      setData(res.data)
      setMeta({
        cacheStatus: res.cacheStatus ?? null,
        instance: res.instance ?? null,
        at: Date.now(),
      })
      setError(null)
    } catch (err) {
      if (err?.name === 'AbortError') return
      setError(err)
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    const controller = new AbortController()
    let timer = null

    const schedule = () => {
      clearTimeout(timer)
      if (document.visibilityState !== 'visible') return
      timer = setTimeout(async () => {
        await tick(controller.signal)
        schedule()
      }, intervalMs)
    }

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        // Refresh immediately on return, then resume the interval -- a user
        // coming back to the tab should not stare at a stale map.
        tick(controller.signal).then(schedule)
      } else {
        clearTimeout(timer)
      }
    }

    tick(controller.signal).then(schedule)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      controller.abort()
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [enabled, intervalMs, tick])

  const refresh = useCallback(() => tick(undefined), [tick])

  return { data, meta, error, loading, refresh }
}

/**
 * Milliseconds remaining until `targetIso`, measured against the SERVER clock.
 * Returns null while the clock is still syncing or there is no target.
 */
export function useCountdown(targetIso) {
  const { serverNow, synced } = useClock()
  const [remaining, setRemaining] = useState(null)

  useEffect(() => {
    if (!targetIso || !synced) {
      setRemaining(null)
      return undefined
    }
    const target = new Date(targetIso).getTime()
    const update = () => setRemaining(Math.max(0, target - serverNow()))
    update()
    // 250 ms rather than 1000 ms so the displayed second never looks stuck
    // when the tick and the wall clock drift apart.
    const id = setInterval(update, 250)
    return () => clearInterval(id)
  }, [targetIso, synced, serverNow])

  return remaining
}

/** 95000 -> "01:35";  3725000 -> "1h 02m" */
export function formatDuration(ms) {
  if (ms == null) return '--:--'
  const total = Math.ceil(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

/** "2026-11-12T06:13:05+00:00" -> "12 Nov 2026, 11:43" in the viewer's zone */
export function formatDateTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString(undefined, {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}
