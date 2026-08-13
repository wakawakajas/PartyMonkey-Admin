// Runs even when the app is closed. Its only job is to turn a push into a
// notification and to bring the app up when that notification is tapped.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

// Chrome looks for a fetch handler before it offers "Install app" on Android.
// This one deliberately does nothing: every request goes to the network as
// normal, so a deploy is never served from a stale cache.
self.addEventListener('fetch', () => {});

// n buzzes of the given length, with a gap between them. The pattern alternates
// buzz/pause and must not end on a pause, or some phones sit through it.
function buzz(n, ms) {
  const out = [];
  for (let i = 0; i < n; i++) {
    if (i) out.push(90);
    out.push(ms);
  }
  return out;
}

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
    //
    // The pattern counts, the way the beeps do inside the app: two buzzes for
    // something arriving, eight for a reminder you already put off. A push
    // cannot carry a sound — that belongs to the phone's own settings for this
    // app — but it can carry this, and counting works through a pocket without
    // having to recognise anything. Urgent buzzes longer without changing the
    // count, so the two readings do not fight each other.
    vibrate: buzz(d.kind === 'reminder' ? 8 : 2, d.urgent ? 200 : 110),
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
