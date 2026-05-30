// Lokki dashboard service worker — minimal shell cache.
//
// What this does:
//   - On install: pre-cache the dashboard shell (HTML + manifest + icon).
//   - On fetch: network-first for everything, with cache fallback when
//     the network is unreachable (coord rebooting, brief WiFi flake,
//     phone driving from a moving vehicle into a dead zone, etc.).
//   - /api/* requests bypass the worker entirely — those are live
//     data and must never be served from cache.
//
// What this deliberately does NOT do:
//   - Background sync, push notifications, periodic-sync — none of
//     them are useful without a public endpoint, and Lokki is
//     LAN-only by design.
//   - Cache /api/config or /api/fleet for offline read. Cached fleet
//     state would mislead more than it helps — "Leaf 3 last seen 2 s
//     ago" is a lie if you opened the dashboard 4 hours later.
//   - Force-refresh on activation. The cache bumps to a new name when
//     CACHE_VERSION changes; old caches get deleted in `activate`.
//     For routine cache busts (new firmware → new HTML), bump the
//     version string below.

const CACHE_VERSION = 'lokki-shell-v1';
const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icon.svg',
];

self.addEventListener('install', (e) => {
  // skipWaiting so a new SW activates immediately rather than waiting
  // for every tab to close. The cache-versioning above prevents stale
  // shell delivery; skipWaiting just shortens the upgrade cycle.
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_VERSION).then((c) => c.addAll(SHELL_ASSETS))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Hands off to the network for anything API-shaped — fleet state,
  // config, scenes, sun-times, events. Caching these would mislead.
  if (url.pathname.startsWith('/api/')) {
    return;   // browser default: go to network
  }
  // Anything not GET passes through too (PATCH/POST/DELETE).
  if (e.request.method !== 'GET') {
    return;
  }

  // Network-first with cache fallback. Online → fresh; offline →
  // the install-time-cached shell. For navigations specifically we
  // fall back to /index.html so the dashboard at least renders its
  // chrome even if a sub-resource is missing.
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        // Mirror successful GETs to the cache for the next offline
        // load. Only same-origin to avoid filling cache with vendor
        // CDN responses that we never asked to pre-cache.
        if (resp.ok && url.origin === self.location.origin) {
          const copy = resp.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(e.request, copy));
        }
        return resp;
      })
      .catch(() =>
        caches.match(e.request).then((cached) => {
          if (cached) return cached;
          if (e.request.mode === 'navigate') {
            return caches.match('/index.html');
          }
          // No cache, no network — let the fetch fail naturally.
          return Response.error();
        })
      )
  );
});
