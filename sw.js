// Runs even when the app is closed. Its only job is to turn a push into a
// notification and to bring the app up when that notification is tapped.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

// Chrome looks for a fetch handler before it offers "Install app" on Android.
// This one deliberately does nothing: every request goes to the network as
// normal, so a deploy is never served from a stale cache.
self.addEventListener('fetch', () => {});

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = {}; }
  const title = d.title || 'Pigu Assistant';
  event.waitUntil(self.registration.showNotification(title, {
    body: d.body || '',
    icon: 'icon-192.png',
    badge: 'icon-192.png',
    // one notification per order, so three updates on the same order replace
    // each other rather than stacking up
    tag: d.tag || 'pickup',
    renotify: true,
    // Android decides whether to show a banner or just drop it in the shade.
    // A vibration pattern and staying put until it is dealt with are what push
    // it towards a banner; without them Samsung tends to file it silently.
    // An urgent request buzzes longer and harder, so it is tellable apart from
    // an ordinary one through a pocket, before the phone is even looked at.
    vibrate: d.urgent ? [200, 90, 200, 90, 200] : [80, 40, 80],
    requireInteraction: true,
    silent: false,
    timestamp: Date.now(),
    data: { url: d.url || './' }
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || './',
                         self.location.href).href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of open) {
      if (c.url.startsWith(self.registration.scope) && 'focus' in c) return c.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
