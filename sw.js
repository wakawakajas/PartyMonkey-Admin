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
    // One short buzz, not a pattern that counts anything. It is here for
    // Samsung rather than for the reader: One UI leans on a vibration pattern
    // when deciding whether to raise a banner or file the notification
    // silently in the shade, and with no pattern at all it tends to file it.
    // Kept to a single 120ms pulse so it registers without being the buzzing
    // that was asked to go away.
    vibrate: [120],
    requireInteraction: true,
    silent: false,
    timestamp: Date.now(),
    data: { url: d.url || './', requestId: d.request_id || null }
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = new URL(data.url || './', self.location.href).href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of open) {
      if (!c.url.startsWith(self.registration.scope) || !('focus' in c)) continue;
      // An app already running will not re-read the address, so the id is
      // handed to it directly. Focus first: on Android the message can be
      // dropped if the page is still in the background when it arrives.
      await c.focus();
      if (data.requestId) c.postMessage({ type: 'open-request', requestId: data.requestId });
      return;
    }
    // nothing running, so the id travels in the address instead
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
