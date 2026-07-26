// Fiduciary service worker (2026-07-26 notifications work).
//
// Exists for two reasons: (1) it's what makes the Push API available at all on iOS Safari
// (16.4+) once this app is added to the Home Screen -- an ordinary open tab can never receive
// push there, only an installed PWA controlled by a service worker can; (2) it's what actually
// receives a push event and turns it into a native OS notification on every platform.
//
// Deliberately NOT a caching/offline service worker -- this app always needs a live connection to
// its own server (portfolio data, chat, everything), so there is no offline mode to get right, and
// no cache-invalidation complexity to introduce for zero benefit.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    data = { title: 'Fiduciary', body: event.data ? event.data.text() : 'New alert' };
  }
  const title = data.title || 'Fiduciary alert';
  const options = {
    body: data.body || '',
    tag: data.tag || 'fiduciary-alert',
    renotify: true,
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
      return undefined;
    })
  );
});
