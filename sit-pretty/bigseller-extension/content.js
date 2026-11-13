// Puts a camera button beside every Shopee order number on BigSeller pages.
// Faded = Sit Pretty has no video for that order yet; bright with a number = it has videos.
// Click: read the "Scan to ship" time from BigSeller's own Order Log (read only), then open the pop-up.
(() => {
  const RE = /^2\d{5}[0-9A-Z]{8}$/;            // Shopee SG order number, e.g. 261104DN337RDT
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'SELECT', 'OPTION', 'NOSCRIPT']);
  const done = new WeakMap();                   // text node -> its button
  const counts = {};                            // order -> number of videos (from Sit Pretty)
  let asked = new Set(), timer = null, haveTimer = null;

  const css = document.createElement('style');
  css.textContent = `.sp-cam{display:inline-flex;align-items:center;gap:2px;margin-left:5px;padding:0 5px;height:18px;border-radius:9px;
    font:600 11px/18px system-ui,sans-serif;cursor:pointer;vertical-align:middle;user-select:none;background:#f0f0f3;color:#8a8a96;
    border:1px solid #e2e2e8;opacity:.75;white-space:nowrap}
    .sp-cam:hover{opacity:1;border-color:#999}
    .sp-cam.has{background:#1d1d1f;color:#fff;border-color:#1d1d1f;opacity:1}
    .sp-cam.wait{opacity:.5;cursor:progress}
    .sp-cam svg{width:12px;height:12px;fill:currentColor}`;
  (document.head || document.documentElement).appendChild(css);
  const ICON = '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v2.5l4.2-2.8A1 1 0 0 1 22 7.5v9a1 1 0 0 1-1.8.8L16 14.5V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

  function paint(btn) {
    const n = counts[btn.dataset.order] || 0;
    btn.classList.toggle('has', n > 0);
    btn.innerHTML = ICON + (n ? n : '');
    btn.title = n ? `Sit Pretty: ${n} video${n > 1 ? 's' : ''} for this order. Click to watch.` : 'Sit Pretty: no video yet. Click to get the packing clip.';
  }

  function makeBtn(order) {
    const b = document.createElement('span');
    b.className = 'sp-cam'; b.dataset.order = order; b.setAttribute('role', 'button'); b.tabIndex = 0;
    paint(b);
    const stop = e => { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); };
    ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'dblclick'].forEach(t => b.addEventListener(t, stop, true));
    b.addEventListener('click', e => { stop(e); openOrder(b); }, true);
    b.addEventListener('keydown', e => { if (e.key === 'Enter') { stop(e); openOrder(b); } }, true);
    return b;
  }

  function scan() {
    timer = null;
    if (!document.body) return;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        const p = n.parentElement;
        if (!p || SKIP.has(p.tagName) || p.isContentEditable || p.closest('.sp-cam')) return NodeFilter.FILTER_REJECT;
        return RE.test(n.nodeValue.trim()) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
    });
    const fresh = [];
    let n;
    while ((n = w.nextNode())) {
      const order = n.nodeValue.trim();
      const old = done.get(n);
      if (old && old.isConnected && old.dataset.order === order) continue;
      if (n.nextSibling && n.nextSibling.classList && n.nextSibling.classList.contains('sp-cam')) { done.set(n, n.nextSibling); continue; }
      const b = makeBtn(order);
      n.parentNode.insertBefore(b, n.nextSibling);
      done.set(n, b);
      if (!(order in counts)) fresh.push(order);
    }
    if (fresh.length) { fresh.forEach(o => asked.add(o)); askHave(); }
  }

  function askHave() {
    clearTimeout(haveTimer);
    haveTimer = setTimeout(() => {
      const orders = [...asked]; asked = new Set();
      if (!orders.length) return;
      chrome.runtime.sendMessage({ type: 'have', orders }, res => {
        if (chrome.runtime.lastError || !res || res.error) return;   // Sit Pretty off or PIN not set: buttons stay faded
        orders.forEach(o => { counts[o] = res.have[o] || 0; });
        document.querySelectorAll('.sp-cam').forEach(b => { if (orders.includes(b.dataset.order)) paint(b); });
      });
    }, 400);
  }

  // ---- BigSeller's own Order Log (read only) for the "Scan to ship" time
  const MON = { Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12 };
  function bsTime(s) {   // "04 Nov 2026 15:17:56" -> "2026-11-04T15:17"
    const m = /(\d{1,2}) (\w{3}) (\d{4}) (\d{2}):(\d{2})/.exec(s || '');
    if (!m || !MON[m[2]]) return '';
    return `${m[3]}-${String(MON[m[2]]).padStart(2, '0')}-${m[1].padStart(2, '0')}T${m[4]}:${m[5]}`;
  }
  async function scanTime(order) {
    try {
      const r = await fetch('/api/v1/order/new/pageList.json', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ allOrder: true, desc: 1, historyOrder: 0, inquireType: 2, orderBy: 'createTime', pageNo: 1, pageSize: 5,
          searchContent: order, searchType: 'orderNo', status: '', timeType: 1, virtualProductFilterType: 0 })
      });
      const j = await r.json();
      const row = ((j.data && j.data.page && j.data.page.rows) || []).find(x => x.platformOrderId === order);
      if (!row) return '';
      const lg = await (await fetch('/api/v1/order/operate/log.json?id=' + row.id, { credentials: 'include' })).json();
      const ev = (lg.data || []).flatMap(s => [...(s.beforeOperateLogDetails || []), ...(s.afterOperateLogDetails || [])]);
      const pick = re => ev.filter(d => re.test(d.contentKey || '')).map(d => bsTime(d.operateDate)).filter(Boolean).sort().pop() || '';
      return pick(/Scan to ship/i) || pick(/Manually shipped|Mark(ed)? as shipped/i);
    } catch (e) { return ''; }
  }

  async function openOrder(btn) {
    if (btn.classList.contains('wait')) return;
    btn.classList.add('wait');
    const scan = await scanTime(btn.dataset.order);
    chrome.runtime.sendMessage({ type: 'open', order: btn.dataset.order, scan }, () => {
      btn.classList.remove('wait');
      // the counts may have changed once the pop-up is used: refresh this order's later
      setTimeout(() => { asked.add(btn.dataset.order); askHave(); }, 60000);
    });
  }

  // Returns' ship-scan times, looked up in BigSeller's own Order Log (read only) and sent to Sit Pretty,
  // so packing clips get cut by themselves and phones open the packing without typing a time.
  function syncScans() {
    chrome.runtime.sendMessage({ type: 'scanwant' }, async res => {
      if (chrome.runtime.lastError || !res || !res.orders || !res.orders.length) return;
      const scans = {}, tried = [];
      for (const o of res.orders.slice(0, 15)) {
        const s = await scanTime(o);
        if (s) scans[o] = s; else tried.push(o);
        await new Promise(r => setTimeout(r, 1500));
      }
      chrome.runtime.sendMessage({ type: 'scans', scans, tried }, () => void chrome.runtime.lastError);
    });
  }
  if (window === window.top) { setTimeout(syncScans, 8000); setInterval(syncScans, 15 * 60000); }

  new MutationObserver(() => { if (!timer) timer = setTimeout(scan, 500); })
    .observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  scan();
  // refresh the colours every 2 minutes (new videos made meanwhile)
  setInterval(() => { Object.keys(counts).forEach(o => asked.add(o)); askHave(); }, 120000);
})();
