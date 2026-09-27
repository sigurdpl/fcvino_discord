/* The whole service worker. It deliberately does almost nothing.
 *
 * Two reasons it exists at all. Android Chrome will not treat a site as
 * installable — as opposed to a bookmark with an icon — without a worker that
 * handles fetches. And when the club's server is not reachable, which while
 * this is served from a laptop is most of the time, somebody who tapped the
 * icon should be told that in the club's own words rather than shown Chrome's
 * dinosaur or Safari's "cannot connect to the server".
 *
 * What it does NOT do is cache the site. A worker that serves pages from a
 * cache is a worker that serves last week's page to somebody in the middle of
 * an evening, and there is no visible sign it has happened. So: everything
 * goes to the network, untouched, every time. The only thing held here is one
 * page that never changes, and it is shown only when the network has already
 * failed.
 */

const SHELL = 'fcvino-offline-1';
const OFFLINE = '/offline';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.add(OFFLINE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  // Older shells, from before the offline page last changed.
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== SHELL).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  // Only whole-page loads. A failed stylesheet or photograph should stay
  // failed; substituting anything for it would be a lie about what loaded.
  if (event.request.mode !== 'navigate') return;

  event.respondWith(
    fetch(event.request).catch(function () {
      return caches.match(OFFLINE, { ignoreSearch: true });
    })
  );
});
