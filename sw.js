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
    // No vibration at all: an empty pattern rather than the field left out,
    // because leaving it out lets Android fall back to the channel's own
    // buzzing. The sound stays with the phone's settings for this app, which
    // is the only place a push has ever been able to leave it.
    //
    // The cost is worth knowing: a vibration pattern is one of the things that
    // pushes Android towards showing a banner rather than filing the
    // notification silently in the shade. requireInteraction below is now
    // carrying that on its own, and Samsung in particular may be quieter.
    vibrate: [],
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
