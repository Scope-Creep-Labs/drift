// Single source of truth for the API base URL the SPA hits.
//
// Vite's `base` option affects asset URLs (with `base: './'` they're
// emitted relative to index.html) but `import.meta.env.BASE_URL` is
// still a compile-time constant of '/'. To make ONE build work at any
// served path (`/`, `/drift/`, `/observability/drift/`, …) we resolve
// API URLs against `document.baseURI`, which Vite sets to the actual
// serving path via the `<base href>` tag it injects on every page.
//
// VITE_API_BASE still wins if set (e.g. Vite dev with a different host).

export function apiBase(): string {
  const override = import.meta.env.VITE_API_BASE as string | undefined
  if (override) return override.replace(/\/$/, '')
  // document.baseURI ends in `/` (it's a directory). new URL('api', …)
  // joins correctly. toString() returns absolute URL; we strip the
  // trailing slash for callers that append `/foo`.
  return new URL('api', document.baseURI).toString().replace(/\/$/, '')
}

export function deployApiBase(): string {
  return apiBase() + '/deploy'
}

// WebSocket URL with the same prefix, scheme upgraded to ws(s).
export function deployWsBase(): string {
  const u = new URL('api/deploy/', document.baseURI)
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'
  return u.toString().replace(/\/$/, '')
}
