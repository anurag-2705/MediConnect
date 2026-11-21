/* MediConnect service worker.
 * Deliberately small and conservative: it makes the app installable and shows a friendly page when the
 * network is down. It never caches API responses, so patient data is never stored by the worker. */
const VERSION = 'mediconnect-v1'
const OFFLINE_URL = '/offline.html'

self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.add(OFFLINE_URL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== VERSION).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', event => {
  const { request } = event
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return

  // Pages: always try the network first; fall back to the offline page only when it fails.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)))
    return
  }

  // Built assets have content hashes in their names, so a cached copy is always the right copy.
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(caches.open(VERSION).then(async cache => {
      const cached = await cache.match(request)
      if (cached) return cached
      const response = await fetch(request)
      if (response.ok) cache.put(request, response.clone())
      return response
    }))
  }
})
