'use strict';
/* cfv-site istemcisi: yatay zaman çizgisi + çevresinde kuvvet yerleşimli ağ (canvas, d3-force). */

const $ = (s, el = document) => el.querySelector(s);
const DAY = 864e5;
const MONTHS = ['Oca', 'Şub', 'Mar', 'Nis', 'May', 'Haz', 'Tem', 'Ağu', 'Eyl', 'Eki', 'Kas', 'Ara'];
const toT = d => Date.parse(d + 'T12:00:00Z');
const toD = t => new Date(t).toISOString().slice(0, 10);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtD = d => { const x = new Date(toT(d)); return `${x.getUTCDate()} ${MONTHS[x.getUTCMonth()]} ${x.getUTCFullYear()}`; };
const fmtN = n => n >= 1000 ? (n / 1000).toFixed(n >= 1e4 ? 0 : 1) + 'b' : String(n);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

async function api(path, opt) {
  const r = await fetch(path, { credentials: 'same-origin', ...opt });
  if (r.status === 401) { location.href = '/login'; throw new Error('giriş'); }
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

const TYPES = {
  person:   { label: 'Kişi',   color: '--person',   band: -1.0, r: [5, 18] },
  chat:     { label: 'Grup',   color: '--chat',     band: -2.0, r: [6, 15] },
  calendar: { label: 'Takvim', color: '--calendar', band: 1.0,  r: [4, 12] },
  photos:   { label: 'Foto',   color: '--photos',   band: 1.9,  r: [5, 17] },
  note:     { label: 'Not',    color: '--note',     band: 2.7,  r: [3, 7] },
};
const SRC = { contacts: 'Rehber', whatsapp: 'WhatsApp', 'iphone-photos': 'Fotoğraflar', email: 'E-posta' };
const KIND_TOT = { whatsapp: 'mesaj', photo: 'foto', video: 'video', calendar: 'etkinlik', note: 'not', email: 'e-posta' };
const totalsText = t => Object.entries(t || {}).filter(([, n]) => n).sort((a, b) => b[1] - a[1])
  .map(([k, n]) => `${fmtN(n)} ${KIND_TOT[k] || k}`).join(' · ');

// ------------------------------------------------------------------ durum
const S = {
  meta: null, t0: 0, t1: 1, W: 0, H: 0, dpr: 1,
  nodes: new Map(), links: [], density: { step: 'day', buckets: [] },
  hidden: new Set(), hover: null, selected: null, seq: 0, colors: {}, thumbs: new Map(),
};
const cv = $('#cv'), ctx = cv.getContext('2d');
const axisY = () => Math.round(S.H * 0.5);
const xOf = t => (t - S.t0) / (S.t1 - S.t0) * S.W;
const tOf = x => S.t0 + x / S.W * (S.t1 - S.t0);
const bandGap = () => Math.max(46, S.H * 0.13);

// ------------------------------------------------------------------ kuvvetler
const sim = d3.forceSimulation([])
  .alphaDecay(0.035).velocityDecay(0.38)
  .force('charge', d3.forceManyBody().strength(d => (d.type === 'person' ? -55 : -28) * Math.min(1, S.W / 1200)).distanceMax(260))
  .force('collide', d3.forceCollide(d => d.r + 3).iterations(2))
  .force('link', d3.forceLink([]).id(d => d.id).distance(46).strength(l => l.k === 'member' ? 0.008 : 0.02))
  .force('anchor', alpha => {
    const ay = axisY(), g = bandGap();
    for (const d of sim.nodes()) {
      const tx = xOf(d.tt);
      const kx = d.type === 'person' || d.type === 'chat' ? 0.07 : 0.16;
      d.vx += (tx - d.x) * kx * alpha;
      const ty = ay + d.band * g;
      d.vy += (ty - d.y) * 0.18 * alpha;
      // ekran içinde kal
      if (d.y < d.r + 6) d.vy += (d.r + 6 - d.y) * 0.5;
      if (d.y > S.H - d.r - 24) d.vy -= (d.y - (S.H - d.r - 24)) * 0.5;
      // çizginin kendi tarafında kal
      const gap = 44 + d.r;  // eksen + yoğunluk şeridi + tarih etiketleri boş kalsın
      if (d.band < 0 && d.y > ay - gap) d.vy -= (d.y - (ay - gap)) * 0.5;
      if (d.band > 0 && d.y < ay + gap + 14) d.vy += ((ay + gap + 14) - d.y) * 0.5;
    }
  })
  .on('tick', draw);

// ------------------------------------------------------------------ görünüm
function setView(t0, t1, keepNodes = true) {
  const first = toT(S.meta.first) - 60 * DAY, last = toT(S.meta.last) + 60 * DAY;
  let span = Math.min(Math.max(t1 - t0, 4 * DAY), (last - first) * 1.15);
  let c = (t0 + t1) / 2;
  c = Math.min(Math.max(c, first + span * 0.2), last - span * 0.2);
  const n0 = c - span / 2, n1 = c + span / 2;
  if (keepNodes) {  // düğümler zamandaki yerlerinde kalsın: ekran x'ini yeni ölçeğe taşı
    for (const d of S.nodes.values()) {
      const t = tOf(d.x);
      d.x = (t - n0) / (n1 - n0) * S.W;
    }
  }
  S.t0 = n0; S.t1 = n1;
  try { history.replaceState(null, '', `#${toD(n0)}..${toD(n1)}`); } catch (e) { /* yok say */ }
  $('#span').textContent = `${fmtD(toD(n0))} – ${fmtD(toD(n1))}`;
  scheduleFetch();
  draw();
}

let fetchTimer = 0;
function scheduleFetch() { clearTimeout(fetchTimer); fetchTimer = setTimeout(fetchWindow, 160); }

async function fetchWindow() {
  const span = S.t1 - S.t0, seq = ++S.seq;
  const d0 = toD(S.t0 - span * 0.12), d1 = toD(S.t1 + span * 0.12);
  const [w, dens] = await Promise.all([
    api(`/api/window?from=${d0}&to=${d1}`),
    api(`/api/density?from=${toD(S.t0 - span)}&to=${toD(S.t1 + span)}`),
  ]);
  if (seq !== S.seq) return;  // daha yeni istek var
  S.density = dens;
  mergeData(w);
}

function jitter(id) {  // kimlikten kararlı 0..1: aynı düğüm her yüklemede aynı yükseklikte
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}

function radius(d, maxW) {
  const [a, b] = TYPES[d.type].r;
  return a + (b - a) * Math.sqrt(Math.min(1, d.w / (maxW || 1)));
}

function mergeData(w) {
  const ay = axisY(), g = bandGap();
  const maxW = {};
  for (const n of w.nodes) maxW[n.type] = Math.max(maxW[n.type] || 0, n.w);
  const next = new Map();
  for (const n of w.nodes) {
    if (S.hidden.has(n.type)) continue;
    let d = S.nodes.get(n.id);
    const tt = toT(n.type === 'photos' ? n.tmid : n.t);
    if (!d) {
      d = { x: xOf(tt) + (Math.random() - .5) * 20, y: ay + TYPES[n.type].band * g + (Math.random() - .5) * g * 0.8, vx: 0, vy: 0 };
    }
    Object.assign(d, n, { tt, band: TYPES[n.type].band * (1 + 0.55 * jitter(n.id)) });
    d.r = radius(d, maxW[n.type]);
    next.set(n.id, d);
  }
  S.nodes = next;
  S.links = w.links.filter(l => next.has(l.s) && next.has(l.t)).map(l => ({ source: l.s, target: l.t, k: l.k, n: l.n }));
  S.adj = new Map();
  for (const l of S.links) {
    for (const [a, b] of [[l.source, l.target], [l.target, l.source]]) {
      if (!S.adj.has(a)) S.adj.set(a, new Set());
      S.adj.get(a).add(b);
    }
  }
  sim.nodes([...next.values()]);
  sim.force('link').links(S.links);
  sim.alpha(Math.max(sim.alpha(), 0.5)).restart();
}

// ------------------------------------------------------------------ çizim
let drawQueued = false;
function draw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; paint(); });
}

function ticks() {
  const span = (S.t1 - S.t0) / DAY, out = [];
  const d0 = new Date(S.t0), d1 = new Date(S.t1);
  if (span > 1500) {
    for (let y = d0.getUTCFullYear(); y <= d1.getUTCFullYear() + 1; y++) out.push({ t: Date.UTC(y, 0, 1), label: String(y), major: true });
  } else if (span > 200) {
    for (let y = d0.getUTCFullYear(), m = d0.getUTCMonth(); Date.UTC(y, m, 1) <= S.t1 + 31 * DAY; m++) {
      if (m > 11) { m = 0; y++; }
      const step = span > 700 ? 3 : 1;
      if (m % step === 0) out.push({ t: Date.UTC(y, m, 1), label: m === 0 ? String(y) : MONTHS[m], major: m === 0 });
    }
  } else if (span > 45) {
    let t = Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth(), d0.getUTCDate());
    t -= ((new Date(t).getUTCDay() + 6) % 7) * DAY;
    for (; t <= S.t1; t += 7 * DAY) {
      const x = new Date(t);
      out.push({ t, label: `${x.getUTCDate()} ${MONTHS[x.getUTCMonth()]}`, major: x.getUTCDate() <= 7 });
    }
  } else {
    const every = span > 20 ? 2 : 1;
    for (let t = Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth(), d0.getUTCDate()); t <= S.t1; t += DAY) {
      const x = new Date(t);
      if ((x.getUTCDate() - 1) % every) continue;
      out.push({ t, label: x.getUTCDate() === 1 ? `1 ${MONTHS[x.getUTCMonth()]}` : String(x.getUTCDate()), major: x.getUTCDate() === 1 });
    }
  }
  return out;
}

function thumb(mid) {
  let img = S.thumbs.get(mid);
  if (!img) {
    img = new Image();
    img.onload = draw;
    img.src = `/api/thumb/${mid}?s=240`;
    S.thumbs.set(mid, img);
    if (S.thumbs.size > 400) S.thumbs.delete(S.thumbs.keys().next().value);
  }
  return img.complete && img.naturalWidth ? img : null;
}

function paint() {
  const { W, H } = S, ay = axisY(), C = S.colors;
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);

  // yoğunluk şeridi: üstte mesaj, altta foto
  const bs = S.density.buckets;
  if (bs.length) {
    const stepMs = (S.density.step === 'week' ? 7 : 1) * DAY;
    const bw = Math.max(1, xOf(S.t0 + stepMs) - xOf(S.t0) - (stepMs / (S.t1 - S.t0) * W > 4 ? 1 : 0));
    let mw = 1, mp = 1;
    for (const b of bs) { mw = Math.max(mw, b.whatsapp || 0); mp = Math.max(mp, b.photo || 0); }
    for (const b of bs) {
      const x = xOf(Date.parse(b.d + 'T00:00:00Z'));
      if (x + bw < 0 || x > W) continue;
      if (b.whatsapp) { ctx.fillStyle = C.chatA; const h = Math.sqrt(b.whatsapp / mw) * 34; ctx.fillRect(x, ay - 2 - h, bw, h); }
      if (b.photo) { ctx.fillStyle = C.photosA; const h = Math.sqrt(b.photo / mp) * 34; ctx.fillRect(x, ay + 2, bw, h); }
    }
  }

  // eksen + işaretler
  ctx.strokeStyle = C.line2; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(0, ay); ctx.lineTo(W, ay); ctx.stroke();
  ctx.font = '11.5px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const tk of ticks()) {
    const x = xOf(tk.t);
    if (x < -40 || x > W + 40) continue;
    ctx.strokeStyle = tk.major ? C.line2 : C.line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x + .5, ay - (tk.major ? 7 : 4)); ctx.lineTo(x + .5, ay + (tk.major ? 7 : 4)); ctx.stroke();
    ctx.fillStyle = tk.major ? C.text : C.muted;
    ctx.fillText(tk.label, x, ay + 40);
  }

  const focus = S.hover || S.selected;
  const near = focus ? (S.adj?.get(focus.id) || new Set()) : null;
  const lit = d => !focus || d === focus || near.has(d.id);

  // seçili/üzerindeki düğümün zamanı: çizgiye ince sap
  if (focus) {
    for (const d of [focus, ...[...near].map(id => S.nodes.get(id)).filter(Boolean)]) {
      ctx.strokeStyle = C[d.type + 'A']; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(xOf(d.tt), ay); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // bağlar
  for (const l of S.links) {
    const a = l.source, b = l.target;
    if (typeof a !== 'object') continue;
    const on = focus && (a === focus || b === focus);
    ctx.strokeStyle = on ? C.linkOn : C.link; ctx.lineWidth = on ? 1.4 : 1;
    ctx.globalAlpha = focus && !on ? 0.35 : 1;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // düğümler
  for (const d of S.nodes.values()) {
    ctx.globalAlpha = lit(d) ? 1 : 0.28;
    const col = C[d.type];
    if (d.type === 'calendar') {
      const s = d.r * 1.6;
      ctx.fillStyle = col; roundRect(d.x - s / 2, d.y - s / 2, s, s, 3); ctx.fill();
    } else if (d.type === 'photos') {
      const img = d.r >= 9 ? thumb(d.media) : null;
      ctx.save(); ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      if (img) { ctx.clip(); const s = d.r * 2; ctx.drawImage(img, d.x - d.r, d.y - d.r, s, s); }
      else { ctx.fillStyle = col; ctx.fill(); }
      ctx.restore();
      ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2); ctx.stroke();
    } else if (d.type === 'note') {
      ctx.fillStyle = col; ctx.beginPath();
      ctx.moveTo(d.x, d.y - d.r * 1.3); ctx.lineTo(d.x + d.r * 1.1, d.y); ctx.lineTo(d.x, d.y + d.r * 1.3); ctx.lineTo(d.x - d.r * 1.1, d.y);
      ctx.closePath(); ctx.fill();
    } else {
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2); ctx.fill();
      if (d.type === 'chat') { ctx.fillStyle = C.bg; ctx.beginPath(); ctx.arc(d.x, d.y, d.r * 0.45, 0, Math.PI * 2); ctx.fill(); }
    }
    if (d === S.selected) {
      ctx.strokeStyle = C.text; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(d.x, d.y, d.r + 4, 0, Math.PI * 2); ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;

  // etiketler: önce odak ve komşular, sonra büyükten küçüğe; çakışan etiket çizilmez
  ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.lineJoin = 'round';
  const spanDays = (S.t1 - S.t0) / DAY;
  const prio = d => d === focus ? 1e9 : (near && near.has(d.id)) ? 1e8 + d.r :
    focus ? -1 : d.type === 'person' ? 100 + d.r : d.type === 'chat' ? 90 + d.r :
    d.type === 'calendar' ? (spanDays < 60 || d.r >= 9 ? 50 + d.r : -1) :
    d.type === 'photos' ? (spanDays < 40 ? 20 + d.r : -1) : d.type === 'note' && spanDays < 25 ? 10 : -1;
  const cand = [...S.nodes.values()].map(d => [prio(d), d]).filter(x => x[0] >= 0).sort((a, b) => b[0] - a[0]);
  const boxes = [];
  for (const [, d] of cand) {
    ctx.font = (d.type === 'person' ? '600 ' : '') + '12px system-ui, sans-serif';
    const label = d.label.length > 28 ? d.label.slice(0, 27) + '…' : d.label;
    const y = d.y + d.r * (d.type === 'note' ? 1.3 : d.type === 'calendar' ? 0.8 : 1) + 4;
    const w = ctx.measureText(label).width + 6, box = [d.x - w / 2, y, d.x + w / 2, y + 14];
    if (d !== focus && boxes.some(o => box[0] < o[2] && box[2] > o[0] && box[1] < o[3] && box[3] > o[1])) continue;
    boxes.push(box);
    ctx.strokeStyle = C.bg; ctx.lineWidth = 4; ctx.strokeText(label, d.x, y);
    ctx.fillStyle = lit(d) ? C.text : C.muted; ctx.fillText(label, d.x, y);
  }
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

function resize() {
  const r = cv.getBoundingClientRect();
  S.dpr = window.devicePixelRatio || 1;
  const oldW = S.W || r.width;
  S.W = r.width; S.H = r.height;
  cv.width = Math.round(r.width * S.dpr); cv.height = Math.round(r.height * S.dpr);
  for (const d of S.nodes.values()) d.x *= S.W / oldW;
  sim.force('charge').strength(sim.force('charge').strength());  // genişliğe göre yeniden hesapla
  sim.alpha(Math.max(sim.alpha(), 0.3)).restart();
  draw();
}

function loadColors() {
  const C = S.colors;
  for (const k of ['bg', 'line', 'line2', 'text', 'muted', 'person', 'calendar', 'photos', 'note', 'chat']) C[k] = css('--' + k);
  for (const k of ['person', 'calendar', 'photos', 'note', 'chat']) C[k + 'A'] = hexA(C[k], k === 'chat' || k === 'photos' ? 0.32 : 0.55);
  C.link = hexA(C.muted, 0.22); C.linkOn = hexA(C.text, 0.75);
}
function hexA(hex, a) {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
}

// ------------------------------------------------------------------ etkileşim
function hit(x, y) {
  let best = null, bd = Infinity;
  for (const d of S.nodes.values()) {
    const dd = Math.hypot(d.x - x, d.y - y);
    if (dd < d.r + 5 && dd < bd) { best = d; bd = dd; }
  }
  return best;
}

const ptrs = new Map();
let drag = null, pinch = null;
cv.addEventListener('pointerdown', e => {
  cv.setPointerCapture(e.pointerId);
  ptrs.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
  if (ptrs.size === 2) {
    const [a, b] = [...ptrs.values()];
    pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), t0: S.t0, t1: S.t1, cx: (a.x + b.x) / 2 };
    drag = null; return;
  }
  const n = hit(e.offsetX, e.offsetY);
  drag = { node: n, x: e.offsetX, y: e.offsetY, t0: S.t0, t1: S.t1, moved: false };
  if (n) { n.fx = n.x; n.fy = n.y; sim.alphaTarget(0.25).restart(); }
  cv.classList.add('dragging');
});
cv.addEventListener('pointermove', e => {
  if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
  if (pinch && ptrs.size === 2) {
    const [a, b] = [...ptrs.values()];
    const f = pinch.dist / Math.max(20, Math.hypot(a.x - b.x, a.y - b.y));
    const tc = pinch.t0 + pinch.cx / S.W * (pinch.t1 - pinch.t0);
    setView(tc - (tc - pinch.t0) * f, tc + (pinch.t1 - tc) * f);
    return;
  }
  if (drag) {
    const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.node) { drag.node.fx = e.offsetX; drag.node.fy = e.offsetY; draw(); }
    else if (drag.moved) {
      const dt = dx / S.W * (drag.t1 - drag.t0);
      setView(drag.t0 - dt, drag.t1 - dt);
    }
    return;
  }
  const n = hit(e.offsetX, e.offsetY);
  if (n !== S.hover) { S.hover = n; cv.classList.toggle('hover', !!n); draw(); }
  showTip(n, e.offsetX, e.offsetY);
});
const endPtr = e => {
  ptrs.delete(e.pointerId);
  if (ptrs.size < 2) pinch = null;
  cv.classList.remove('dragging');
  if (!drag) return;
  const d = drag; drag = null;
  if (d.node) { d.node.fx = d.node.fy = null; sim.alphaTarget(0); }
  if (!d.moved && e.type === 'pointerup') {
    if (d.node) select(d.node); else { S.selected = null; closePanel(); draw(); }
  }
};
cv.addEventListener('pointerup', endPtr);
cv.addEventListener('pointercancel', endPtr);
cv.addEventListener('pointerleave', () => { if (!drag) { S.hover = null; showTip(null); draw(); } });
cv.addEventListener('wheel', e => {
  e.preventDefault();
  if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {  // yatay kaydırma
    const dt = (e.deltaX || e.deltaY) / S.W * (S.t1 - S.t0);
    return setView(S.t0 + dt, S.t1 + dt);
  }
  const f = Math.exp(e.deltaY * (e.deltaMode ? 0.05 : 0.0015));
  const tc = tOf(e.offsetX);
  setView(tc - (tc - S.t0) * f, tc + (S.t1 - tc) * f);
}, { passive: false });
cv.addEventListener('dblclick', e => {
  const n = hit(e.offsetX, e.offsetY);
  if (n && n.type === 'photos') return zoomTo(n.from, n.to, 2);
  if (n && n.t0) return zoomTo(n.t0, n.t1, 10);
  const tc = tOf(e.offsetX), span = (S.t1 - S.t0) / 3;
  setView(tc - span / 2, tc + span / 2);
});
window.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || $('#tl').hidden) return;
  const span = S.t1 - S.t0;
  if (e.key === 'ArrowLeft') setView(S.t0 - span * 0.15, S.t1 - span * 0.15);
  else if (e.key === 'ArrowRight') setView(S.t0 + span * 0.15, S.t1 + span * 0.15);
  else if (e.key === '+' || e.key === '=') setView(S.t0 + span * 0.2, S.t1 - span * 0.2);
  else if (e.key === '-') setView(S.t0 - span * 0.25, S.t1 + span * 0.25);
  else if (e.key === 'Escape') { closePanel(); $('#lightbox').hidden = true; }
});

function zoomTo(d0, d1, padDays = 0) {
  setView(toT(d0) - padDays * DAY - DAY / 2, toT(d1) + padDays * DAY + DAY / 2);
}

const tip = $('#tip');
function showTip(d, x, y) {
  if (!d) { tip.hidden = true; return; }
  let sub = '';
  if (d.type === 'person') sub = `${totalsText(d.counts)}<br><small>${fmtD(d.t0)} – ${fmtD(d.t1)}</small>`;
  else if (d.type === 'calendar') sub = `${fmtD(d.t)}${d.allday ? ' · tüm gün' : ''}${d.photos ? ` · ${d.photos} foto` : ''}`;
  else if (d.type === 'photos') sub = d.from === d.to ? fmtD(d.from) : `${fmtD(d.from)} – ${fmtD(d.to)}`;
  else if (d.type === 'chat') sub = `${fmtN(d.n)} mesaj (bu aralıkta)`;
  else if (d.type === 'note') sub = fmtD(d.t);
  tip.innerHTML = `<b>${esc(d.label)}</b><span class="muted">${TYPES[d.type].label}</span> · ${sub}`;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, S.W - r.width - 8) + 'px';
  tip.style.top = Math.max(8, y - r.height - 10) + 'px';
}

// ------------------------------------------------------------------ panel
const panel = $('#panel'), pbody = $('#pbody');
function closePanel() { panel.hidden = true; pbody.innerHTML = ''; }
$('#pclose').onclick = () => { S.selected = null; closePanel(); draw(); };
function openPanel(html) { pbody.innerHTML = html; panel.hidden = false; panel.scrollTop = 0; }

function select(d) {
  S.selected = d; draw();
  const id = d.id.split(':')[1];
  if (d.type === 'person') openPerson(+id);
  else if (d.type === 'calendar' || d.type === 'note') openEvent(+id, d.type);
  else if (d.type === 'photos') openPhotos(d);
  else if (d.type === 'chat') openChat(d);
}

function photoGrid(photos) {
  if (!photos.length) return '<div class="empty">Foto yok.</div>';
  return `<div class="grid">${photos.map(p => `<button data-photo="${p.id}" title="${esc(fmtD(p.d))}">` +
    `<img loading="lazy" src="/api/thumb/${p.id}?s=240" alt=""><span class="d">${esc(p.d.slice(2))}</span></button>`).join('')}</div>`;
}

async function openPerson(cid) {
  openPanel('<div class="empty">Yükleniyor…</div>');
  const p = await api(`/api/person/${cid}`);
  const w = p.weeks;
  openPanel(`
    <div class="kind">Kişi</div>
    <h2>${esc(p.name)}</h2>
    <div class="muted">${totalsText(p.totals) || 'kayıt yok'}</div>
    ${w.length ? `<div class="row"><span class="muted grow">${fmtD(w[0].d)} – ${fmtD(w[w.length - 1].d)}</span>
      <button class="link" data-act="show">Zaman çizgisinde göster</button></div>` : ''}
    <h3>Zaman içinde (haftalık)</h3><canvas class="spark" id="spark"></canvas>
    <div class="muted"><small>üst: mesaj · alt: foto · tıkla: o haftaya git</small></div>
    <h3>Kaynaklar (${p.aliases.length})</h3>
    ${p.aliases.map(a => `<div class="row"><span class="src">${esc(SRC[a.kind] || a.kind)}</span>
      <span class="grow">${esc(a.name)}</span><small>${esc(a.shown)}</small></div>`).join('')}
    <h3>Bağlar</h3>
    ${p.edges.length ? p.edges.map(edgeHtml).join('') : '<div class="empty">Tek kaynak; bağ yok.</div>'}
    ${p.candidates.length ? `<h3>Olası eşleşmeler (${p.candidates.length})</h3>${p.candidates.map(c => candMini(c, cid)).join('')}` : ''}
    <h3>Elle bağla</h3>
    <div class="linkbox"><input id="linkq" placeholder="Aynı kişi olan kaydı ara…" autocomplete="off"><div id="linkr" class="dropdown" hidden></div></div>
    <h3>Fotoğraflar (${p.photos.length}${p.photos.length >= 240 ? '+' : ''})</h3>${photoGrid(p.photos)}
    <h3>Ortak etkinlikler ve notlar</h3>
    <div class="list">${p.events.length ? p.events.map(e => `<button class="item" data-event="${e.id}" data-kind="${e.kind}" data-d="${e.d}">
      <span class="d">${esc(e.d)}</span><span>${esc(e.title || '(adsız)')}</span></button>`).join('') : '<div class="empty">Yok.</div>'}</div>
    ${p.groups.length ? `<h3>Ortak gruplar</h3>${p.groups.map(g => `<div class="row"><span class="grow">${esc(g.name)}</span><small>${fmtN(g.n)} mesaj</small></div>`).join('')}` : ''}
  `);
  $('.kind', pbody).style.color = S.colors.person;
  drawSpark($('#spark'), w);
  pbody.querySelector('[data-act=show]')?.addEventListener('click', () => {
    zoomTo(w[0].d, w[w.length - 1].d, 14);
  });
  wirePanel(cid);
  const lq = $('#linkq'), lr = $('#linkr');
  lq.addEventListener('input', debounce(async () => {
    const q = lq.value.trim();
    if (q.length < 2) { lr.hidden = true; return; }
    const res = (await api(`/api/search?q=${encodeURIComponent(q)}`)).filter(r => r.id !== cid);
    lr.innerHTML = res.length ? res.map(r => `<button data-link="${r.id}"><span>${esc(r.name)}</span><small>${fmtN(r.total)} kayıt</small></button>`).join('')
      : '<div class="empty">&nbsp;Bulunamadı</div>';
    lr.hidden = false;
  }, 150));
  lr.addEventListener('click', async e => {
    const b = e.target.closest('[data-link]');
    if (!b) return;
    if (!confirm(`"${p.name}" ile "${b.querySelector('span').textContent}" aynı kişi olarak bağlansın mı? (Geri alınabilir.)`)) return;
    await post('/api/link', { a: cid, b: +b.dataset.link });
    await afterDecision(cid);
  });
}

function edgeHtml(e) {
  const auto = e.via === 'auto';
  return `<div class="edge"><div><span class="src">${esc(SRC[e.a.kind] || e.a.kind)}</span> ${esc(e.a.name)}
    ↔ <span class="src">${esc(SRC[e.b.kind] || e.b.kind)}</span> ${esc(e.b.name)}</div>
    <div class="how"><span class="${auto ? 'via-auto' : 'via-tuna'}">${auto ? 'otomatik' : 'Tuna onayı'}</span>
    <span>${esc(e.method_label)}${e.method !== 'manual' ? ` · %${Math.round(e.confidence * 100)}` : ''}</span>
    ${auto ? `<button class="bad" data-split='${esc(JSON.stringify({ a: e.a, b: e.b }))}'>Ayır</button>`
      : `<button data-undo="${e.decision_id}">Geri al</button>`}</div></div>`;
}

function candMini(c, cid) {
  const other = c.a.person === cid ? c.b : c.a;
  return `<div class="edge"><div><span class="src">${esc(SRC[other.kind] || other.kind)}</span> ${esc(other.name)}
    <small>· ${totalsText(other.totals) || '—'}</small></div>
    <div class="how"><span>${esc(c.method_label)} · %${Math.round(c.confidence * 100)}</span>
    <button class="ok" data-decide="same" data-pair='${esc(JSON.stringify({ a: c.a, b: c.b }))}'>Aynı kişi</button>
    <button class="bad" data-decide="different" data-pair='${esc(JSON.stringify({ a: c.a, b: c.b }))}'>Farklı</button></div></div>`;
}

function wirePanel(cid) {
  pbody.onclick = async e => {
    const t = e.target.closest('button');
    if (!t) return;
    if (t.dataset.photo) return openLightbox(t.dataset.photo);
    if (t.dataset.event) { zoomTo(t.dataset.d, t.dataset.d, 10); return openEvent(+t.dataset.event, t.dataset.kind); }
    if (t.dataset.person) { S.selected = S.nodes.get('p:' + t.dataset.person) || null; draw(); return openPerson(+t.dataset.person); }
    if (t.dataset.undo) { await post(`/api/decision/${t.dataset.undo}/undo`, {}); return afterDecision(cid); }
    if (t.dataset.split) {
      const p = JSON.parse(t.dataset.split);
      if (!confirm(`${p.a.name} ↔ ${p.b.name} bağı ayrılsın mı? ("Farklı kişi" kararı yazılır, geri alınabilir.)`)) return;
      await post('/api/decision', { a: p.a, b: p.b, verdict: 'different' }); return afterDecision(cid);
    }
    if (t.dataset.decide) {
      const p = JSON.parse(t.dataset.pair);
      await post('/api/decision', { a: p.a, b: p.b, verdict: t.dataset.decide }); return afterDecision(cid);
    }
  };
}

async function afterDecision(cid) {
  await refreshMeta();
  fetchWindow();
  if (!$('#mx').hidden) loadMatches();
  if (cid != null) {
    // kişi kimliği birleşmeyle değişebilir: aynı adı taşıyan grubu bul
    try { await openPerson(cid); } catch (e) { closePanel(); }
  }
}

function drawSpark(c, weeks) {
  const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  c.width = r.width * dpr; c.height = r.height * dpr;
  const g = c.getContext('2d'); g.scale(dpr, dpr);
  if (!weeks.length) return;
  const t0 = Math.min(toT(S.meta.first), toT(weeks[0].d)), t1 = toT(S.meta.last);
  const X = t => (t - t0) / (t1 - t0) * r.width, mid = r.height * 0.55;
  let mw = 1, mp = 1;
  for (const w of weeks) { mw = Math.max(mw, w.whatsapp || 0); mp = Math.max(mp, w.photo || 0); }
  g.fillStyle = hexA(S.colors.muted, 0.12);
  g.fillRect(X(S.t0), 0, Math.max(2, X(S.t1) - X(S.t0)), r.height);
  const bw = Math.max(1, X(t0 + 7 * DAY) - X(t0));
  for (const w of weeks) {
    const x = X(toT(w.d));
    if (w.whatsapp) { g.fillStyle = S.colors.chat; const h = (w.whatsapp / mw) * (mid - 4); g.fillRect(x, mid - 1 - h, bw, h); }
    if (w.photo) { g.fillStyle = S.colors.photos; const h = (w.photo / mp) * (r.height - mid - 4); g.fillRect(x, mid + 1, bw, h); }
  }
  g.fillStyle = S.colors.line2; g.fillRect(0, mid - .5, r.width, 1);
  c.onclick = e => {
    const t = t0 + e.offsetX / r.width * (t1 - t0);
    zoomTo(toD(t - 3 * DAY), toD(t + 3 * DAY), 10);
  };
}

async function openEvent(id, kind) {
  openPanel('<div class="empty">Yükleniyor…</div>');
  const e = await api(`/api/event/${id}`);
  const time = e.time_quality === 'day' ? 'tüm gün' :
    new Date(e.start_utc).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' }) +
    (e.end_utc ? '–' + new Date(e.end_utc).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' }) : '');
  openPanel(`<div class="kind">${kind === 'note' ? 'Not' : 'Takvim'}</div><h2>${esc(e.title || '(adsız)')}</h2>
    <div class="muted">${fmtD(e.local_date)} · ${esc(time)}</div>
    ${e.body ? `<h3>İçerik</h3><div>${esc(e.body).replace(/\n/g, '<br>')}</div>` : ''}
    <h3>Kişiler</h3>${e.people.length ? `<div class="people">${e.people.map(p => `<button data-person="${p.id}">${esc(p.name)}</button>`).join('')}</div>` : '<div class="empty">Yok.</div>'}
    ${kind === 'note' ? '' : `<h3>Bu sırada çekilen fotolar (${e.photos.length})</h3>${photoGrid(e.photos)}`}`);
  $('.kind', pbody).style.color = S.colors[kind === 'note' ? 'note' : 'calendar'];
  wirePanel(null);
}

async function openPhotos(d) {
  openPanel('<div class="empty">Yükleniyor…</div>');
  const ph = await api(`/api/photos?from=${d.from}&to=${d.to}`);
  const ppl = [...(S.adj.get(d.id) || [])].map(id => S.nodes.get(id)).filter(n => n && n.type === 'person');
  openPanel(`<div class="kind">Fotoğraflar</div><h2>${d.n} foto</h2>
    <div class="row"><span class="muted grow">${d.from === d.to ? fmtD(d.from) : `${fmtD(d.from)} – ${fmtD(d.to)}`}</span>
    <button class="link" data-zoom="1">Bu aralığa in</button></div>
    ${ppl.length ? `<h3>Fotolarda</h3><div class="people">${ppl.map(p => `<button data-person="${p.id.slice(2)}">${esc(p.label)}</button>`).join('')}</div>` : ''}
    <h3>Fotolar${ph.length >= 300 ? ' (ilk 300)' : ''}</h3>${photoGrid(ph)}`);
  $('.kind', pbody).style.color = S.colors.photos;
  wirePanel(null);
  pbody.querySelector('[data-zoom]').addEventListener('click', () => zoomTo(d.from, d.to, 1));
}

function openChat(d) {
  const ppl = [...(S.adj.get(d.id) || [])].map(id => S.nodes.get(id)).filter(Boolean);
  openPanel(`<div class="kind">WhatsApp grubu</div><h2>${esc(d.label)}</h2>
    <div class="muted">${fmtN(d.n)} mesaj (görünen aralıkta)</div>
    <h3>Bu aralıkta görünen üyeler</h3>
    ${ppl.length ? `<div class="people">${ppl.map(p => `<button data-person="${p.id.slice(2)}">${esc(p.label)}</button>`).join('')}</div>` : '<div class="empty">Yok.</div>'}`);
  $('.kind', pbody).style.color = S.colors.chat;
  wirePanel(null);
}

function openLightbox(mid) {
  const lb = $('#lightbox');
  $('img', lb).src = `/api/thumb/${mid}?s=720`;
  lb.hidden = false;
}
$('#lightbox').onclick = () => { $('#lightbox').hidden = true; };

// ------------------------------------------------------------------ eşleşmeler sekmesi
let mxStatus = 'pending';
const MX_SEG = [['pending', 'Bekleyen'], ['auto', 'Otomatik'], ['same', 'Onaylanan'], ['different', 'Reddedilen'], ['all', 'Hepsi']];
const ST_LABEL = { pending: 'bekliyor', auto: 'otomatik', same: 'aynı (Tuna)', different: 'farklı (Tuna)', blocked: 'engellendi' };

async function loadMatches() {
  const m = await api(`/api/matches?status=${mxStatus}`);
  $('#mx-seg').innerHTML = MX_SEG.map(([k, l]) => {
    const n = k === 'all' ? Object.values(m.counts).reduce((a, b) => a + b, 0) : (m.counts[k] || 0);
    return `<button data-st="${k}" class="${k === mxStatus ? 'on' : ''}">${l} ${n}</button>`;
  }).join('');
  $('#mx-list').innerHTML = m.items.length ? m.items.map(matchCard).join('') : '<div class="empty">Burada bir şey yok.</div>';
  for (const el of document.querySelectorAll('#mx-list .conf i')) el.style.width = el.dataset.w + '%';
  $('#mx-hist').innerHTML = m.history.length ? m.history.map(h => `<div class="hist ${h.undone_at ? 'undone' : ''}">
    <span class="d">${esc(h.at.slice(0, 16).replace('T', ' '))}</span>
    <span class="st st-${h.verdict}">${h.verdict === 'same' ? 'aynı' : 'farklı'}</span>
    <span class="grow">${esc(h.a.name)} <small>${esc(SRC[h.a.kind] || h.a.kind)}</small> ↔ ${esc(h.b.name)} <small>${esc(SRC[h.b.kind] || h.b.kind)}</small></span>
    ${h.undone_at ? '<small>geri alındı</small>' : `<button data-undo="${h.id}">Geri al</button>`}</div>`).join('')
    : '<div class="empty">Henüz karar yok.</div>';
}

function matchCard(c) {
  const side = s => `<div class="side"><div class="nm"><span class="src">${esc(SRC[s.kind] || s.kind)}</span> ${esc(s.name)}</div>
    <div class="meta">${totalsText(s.totals) || 'kayıt yok'}${s.first ? ` · ${s.first.slice(0, 7)} → ${s.last.slice(0, 7)}` : ''}</div>
    ${s.group_size > 1 ? `<div class="meta">grup: ${esc(s.group)} (${s.group_size} kaynak)</div>` : ''}
    <button class="link" data-person="${s.person}">aç</button></div>`;
  const pair = esc(JSON.stringify({ a: c.a, b: c.b }));
  let acts;
  if (c.status === 'pending') acts = `<button class="ok" data-decide="same" data-pair='${pair}'>Aynı kişi</button>
    <button class="bad" data-decide="different" data-pair='${pair}'>Farklı</button>`;
  else if (c.status === 'auto' || c.status === 'blocked') acts = c.status === 'auto' ? `<button class="bad" data-decide="different" data-pair='${pair}'>Ayır</button>` : '';
  else acts = `<button data-undo="${c.decision_id}">Geri al</button>`;
  return `<div class="mcard">${side(c.a)}
    <div class="mid"><div class="st st-${c.status}">${ST_LABEL[c.status] || c.status}</div>
      <div>${esc(c.method_label)}</div><div class="conf"><i data-w="${Math.round(c.confidence * 100)}"></i></div>
      <small>güven %${Math.round(c.confidence * 100)}${c.also.length ? ` · +${c.also.length} benzer öneri` : ''}</small></div>
    ${side(c.b)}<div class="acts">${acts}</div></div>`;
}

$('#mx').addEventListener('click', async e => {
  const t = e.target.closest('button');
  if (!t) return;
  if (t.dataset.st) { mxStatus = t.dataset.st; return loadMatches(); }
  if (t.dataset.person) { return openPerson(+t.dataset.person); }
  if (t.dataset.undo) { await post(`/api/decision/${t.dataset.undo}/undo`, {}); return afterDecision(null); }
  if (t.dataset.decide) {
    const p = JSON.parse(t.dataset.pair);
    t.disabled = true;
    await post('/api/decision', { a: p.a, b: p.b, verdict: t.dataset.decide });
    return afterDecision(null);
  }
});

// ------------------------------------------------------------------ üst çubuk
function setTab(tab) {
  for (const b of document.querySelectorAll('.tabs button')) b.classList.toggle('on', b.dataset.tab === tab);
  $('#tl').hidden = tab !== 'tl'; $('#mx').hidden = tab !== 'mx';
  closePanel(); S.selected = null;
  if (tab === 'mx') loadMatches(); else { resize(); }
}
for (const b of document.querySelectorAll('.tabs button')) b.onclick = () => setTab(b.dataset.tab);

function buildFilters() {
  $('#filters').innerHTML = Object.entries(TYPES).map(([k, t]) =>
    `<button class="chip on" data-type="${k}"><i data-c="${t.color}"></i>${t.label}</button>`).join('');
  for (const i of document.querySelectorAll('#filters i')) i.style.background = css(i.dataset.c);
  $('#filters').onclick = e => {
    const b = e.target.closest('[data-type]');
    if (!b) return;
    const k = b.dataset.type;
    if (S.hidden.has(k)) S.hidden.delete(k); else S.hidden.add(k);
    b.classList.toggle('on', !S.hidden.has(k));
    fetchWindow();
  };
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

const qi = $('#q'), sr = $('#sr');
let srItems = [];
qi.addEventListener('input', debounce(async () => {
  const q = qi.value.trim();
  srItems = [];
  const m = q.match(/^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$/);
  if (m) {
    const y = +m[1], mo = m[2] ? +m[2] - 1 : null, d = m[3] ? +m[3] : null;
    let a, b;
    if (d) { a = b = toD(Date.UTC(y, mo, d)); }
    else if (mo != null) { a = toD(Date.UTC(y, mo, 1)); b = toD(Date.UTC(y, mo + 1, 0)); }
    else { a = `${y}-01-01`; b = `${y}-12-31`; }
    srItems.push({ label: `Tarihe git: ${d ? fmtD(a) : mo != null ? MONTHS[mo] + ' ' + y : y}`, go: () => zoomTo(a, b, d ? 3 : 0) });
  }
  if (q.length >= 2 && !m) {
    for (const r of await api(`/api/search?q=${encodeURIComponent(q)}`)) {
      srItems.push({ label: r.name, sub: `${fmtN(r.total)} kayıt`, go: () => { setTab('tl'); openPerson(r.id); } });
    }
  }
  sr.innerHTML = srItems.map((it, i) => `<button data-i="${i}" class="${i ? '' : 'on'}"><span>${esc(it.label)}</span><small>${esc(it.sub || '')}</small></button>`).join('');
  sr.hidden = !srItems.length;
}, 140));
qi.addEventListener('keydown', e => {
  if (e.key === 'Enter' && srItems[0]) { srItems[0].go(); sr.hidden = true; qi.blur(); }
  if (e.key === 'Escape') { sr.hidden = true; qi.blur(); }
});
sr.addEventListener('click', e => {
  const b = e.target.closest('[data-i]');
  if (b) { srItems[+b.dataset.i].go(); sr.hidden = true; }
});
document.addEventListener('click', e => { if (!e.target.closest('.search')) sr.hidden = true; });

async function refreshMeta() {
  S.meta = await api('/api/meta');
  const badge = $('#badge');
  badge.textContent = S.meta.pending_matches;
  badge.hidden = !S.meta.pending_matches;
  $('#synth').hidden = !S.meta.synthetic;
}

// ------------------------------------------------------------------ başlangıç
(async function init() {
  loadColors();
  buildFilters();
  await refreshMeta();
  if (!S.meta.first) { $('#span').textContent = 'Veri yok.'; return; }
  resize();
  window.addEventListener('resize', resize);
  const h = location.hash.match(/^#(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/);
  if (h) setView(toT(h[1]), toT(h[2]), false);
  else { const t1 = toT(S.meta.last) + 7 * DAY; setView(t1 - 365 * DAY, t1, false); }
})();
