// Talks to Sit Pretty on the shop network (plain http, which a BigSeller https page may not call itself)
// and opens the order's videos in a small pop-up window.
const DEFAULT_HOST = 'DESKTOP-ABGC9KO:8787';
let popupId = null;

async function cfg() {
  const c = await chrome.storage.local.get(['host', 'pin']);
  return { host: (c.host || DEFAULT_HOST).replace(/^https?:\/\//, '').replace(/\/+$/, ''), pin: c.pin || '' };
}

async function have(orders) {
  const { host, pin } = await cfg();
  const r = await fetch(`http://${host}/api/have`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pin': pin }, body: JSON.stringify({ orders })
  });
  if (r.status === 401) return { error: 'pin' };
  if (!r.ok) return { error: 'http ' + r.status };
  return { have: await r.json() };
}

async function open(order, scan) {
  const { host, pin } = await cfg();
  const url = `http://${host}/order?order=${encodeURIComponent(order)}` +
    (scan ? `&scan=${encodeURIComponent(scan)}` : '') + (pin ? `&pin=${encodeURIComponent(pin)}` : '');
  if (popupId !== null) {
    try {
      const w = await chrome.windows.get(popupId, { populate: true });
      await chrome.tabs.update(w.tabs[0].id, { url });
      await chrome.windows.update(popupId, { focused: true });
      return;
    } catch (e) { popupId = null; }
  }
  const w = await chrome.windows.create({ url, type: 'popup', width: 1040, height: 800, focused: true });
  popupId = w.id;
}

chrome.windows.onRemoved.addListener(id => { if (id === popupId) popupId = null; });

async function scanWant() {
  const { host, pin } = await cfg();
  const r = await fetch(`http://${host}/api/scanwant`, { headers: { 'X-Pin': pin } });
  if (!r.ok) return { orders: [] };
  return await r.json();
}

async function scansPut(scans, tried) {
  const { host, pin } = await cfg();
  const r = await fetch(`http://${host}/api/scans`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pin': pin }, body: JSON.stringify({ scans, tried })
  });
  return r.ok ? await r.json() : { error: 'http ' + r.status };
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === 'scanwant') { scanWant().then(reply, e => reply({ error: String(e.message || e) })); return true; }
  if (msg.type === 'scans') { scansPut(msg.scans, msg.tried).then(reply, e => reply({ error: String(e.message || e) })); return true; }
  if (msg.type === 'have') { have(msg.orders).then(reply, e => reply({ error: String(e.message || e) })); return true; }
  if (msg.type === 'open') { open(msg.order, msg.scan).then(() => reply({ ok: true }), e => reply({ error: String(e.message || e) })); return true; }
  if (msg.type === 'test') { have([]).then(reply, e => reply({ error: String(e.message || e) })); return true; }
});
