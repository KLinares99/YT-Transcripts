"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const main = $("#main");

const STATUS = {
  done: "Transcribed", working: "Working", pending: "Queued",
  no_transcript: "No captions", error: "Failed",
};
const STATUS_ORDER = ["done", "working", "pending", "no_transcript", "error"];
const PALETTE = ["#e5484d", "#3b82f6", "#2f9e5b", "#d99a15", "#8b5cf6", "#0ea5a4", "#ec4899", "#f97316"];

const state = {
  channels: [],
  view: { name: "home" },     // home | channel | search
  filter: { status: "", q: "", sort: "newest", layout: localGet("layout") || "grid" },
  videos: [],
  video: null,                 // open in drawer
};

// ---------- helpers ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }
const fmtNum = n => n == null ? "—" : Intl.NumberFormat("en", { notation: n >= 10000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(n);
function fmtDur(s) {
  if (!s) return "";
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtHours(s) { return s >= 3600 ? (s / 3600).toFixed(s >= 36000 ? 0 : 1) + " h" : Math.round(s / 60) + " min"; }
const thumb = id => `https://i.ytimg.com/vi/${encodeURIComponent(id)}/mqdefault.jpg`;
function colorFor(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[h % PALETTE.length]; }
function avatar(ch, cls = "") {
  const name = ch.title || ch.handle || "?";
  if (ch.avatar) return `<img class="avatar ${cls}" src="${esc(ch.avatar)}" alt="" referrerpolicy="no-referrer">`;
  return `<span class="avatar ${cls}" style="background:${colorFor(name)}">${esc(name.replace(/^@/, "")[0]?.toUpperCase() || "?")}</span>`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3200);
}

function bar(stats, scanning, cls = "") {
  if (scanning && !stats.total) return `<div class="bar scanning ${cls}"></div>`;
  const total = stats.total || 1;
  return `<div class="bar ${cls}">${["done", "working", "no_transcript", "error"]
    .map(s => `<span class="${s}" style="width:${(stats[s] || 0) / total * 100}%"></span>`).join("")}</div>`;
}

// ---------- routing ----------
function route() {
  const h = location.hash.slice(1);
  if (h.startsWith("/c/")) state.view = { name: "channel", id: decodeURIComponent(h.slice(3)) };
  else if (h.startsWith("/search")) state.view = { name: "search", q: new URLSearchParams(h.split("?")[1]).get("q") || "" };
  else state.view = { name: "home" };
  render(true);
}
window.addEventListener("hashchange", route);

// ---------- sidebar ----------
function renderSidebar() {
  const el = $("#channel-list");
  if (!state.channels.length) { el.innerHTML = `<div class="empty-side">No channels yet. Paste a link above to start.</div>`; return; }
  el.innerHTML = state.channels.map(ch => {
    const s = ch.stats, finished = s.done + s.no_transcript + s.error;
    const label = ch.status === "scanning" ? "Cataloguing…" : ch.status === "error" ? "Error" :
      ch.status === "paused" ? `Paused · ${finished}/${s.total}` : `${finished}/${s.total}`;
    const active = state.view.name === "channel" && state.view.id === ch.id ? "active" : "";
    return `<div class="ch-item ${active}" data-ch="${esc(ch.id)}">
      ${avatar(ch)}
      <div class="meta">
        <div class="name">${esc(ch.title || ch.url)}</div>
        ${bar(s, ch.status === "scanning")}
        <div class="sub"><span>${esc(label)}</span><span>${s.total ? Math.round(s.done / s.total * 100) + "%" : ""}</span></div>
      </div></div>`;
  }).join("");
}

// ---------- views ----------
const ICONS = {
  link: '<svg viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M8 6h12M8 12h12M8 18h12"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></svg>',
  text: '<svg viewBox="0 0 24 24"><path d="M4 6h16M4 10h16M4 14h10M4 18h7"/><circle cx="18" cy="17" r="3"/><path d="M20.5 19.5L22 21"/></svg>',
};

function renderHome() {
  main.innerHTML = `<div class="hero">
    <h1>Turn any YouTube channel into searchable text</h1>
    <p>Paste a channel link. The app finds every video, catalogs it, and pulls each transcript in the background.</p>
    <div class="steps">
      <div class="step"><div class="num" style="background:#e5484d">${ICONS.link}</div><h3>1 · Paste a link</h3><p class="muted">@handle, /channel/…, or a playlist</p></div>
      <div class="arrow">→</div>
      <div class="step"><div class="num" style="background:#3b82f6">${ICONS.list}</div><h3>2 · Catalog</h3><p class="muted">Every video's title, date, length and views</p></div>
      <div class="arrow">→</div>
      <div class="step"><div class="num" style="background:#2f9e5b">${ICONS.text}</div><h3>3 · Transcribe & search</h3><p class="muted">Read, search, and export as Markdown, TXT, SRT or JSON</p></div>
    </div>
    <div class="examples">Try: <code>https://www.youtube.com/@3blue1brown</code> &nbsp; or just <code>@veritasium</code></div>
  </div>`;
}

async function renderChannel(fresh) {
  const id = state.view.id;
  let ch;
  try { ch = await api(`/api/channels/${encodeURIComponent(id)}`); }
  catch {
    // Placeholder ids are swapped for the real channel id after cataloguing.
    const real = state.channels.find(c => c.url === state.pendingUrl);
    if (real && real.id !== id) { location.hash = `/c/${encodeURIComponent(real.id)}`; return; }
    main.innerHTML = `<div class="hero"><h1>Channel not found</h1></div>`; return;
  }
  if (state.view.name !== "channel" || state.view.id !== id) return;
  const f = state.filter;
  const qs = new URLSearchParams({ status: f.status, q: f.q, sort: f.sort });
  state.videos = await api(`/api/channels/${encodeURIComponent(id)}/videos?${qs}`);
  const s = ch.stats, finished = s.done + s.no_transcript + s.error;
  const pct = s.total ? Math.round(finished / s.total * 100) : 0;
  const scanning = ch.status === "scanning";

  const head = `
    <div class="ch-head">
      ${avatar(ch, "lg")}
      <div>
        <h1>${esc(ch.title || "Cataloguing channel…")}</h1>
        <div class="handle">${esc(ch.handle || "")}${ch.subscribers ? ` · ${fmtNum(ch.subscribers)} subscribers` : ""}
          · <a href="${esc(ch.url)}" target="_blank" rel="noopener">Open on YouTube ↗</a></div>
      </div>
      <div class="actions">
        ${ch.status === "paused" ? `<button class="btn" data-act="resume">▶ Resume</button>` :
          ch.status === "active" ? `<button class="btn" data-act="pause">❚❚ Pause</button>` : ""}
        <button class="btn" data-act="rescan" title="Look for new uploads">↻ Rescan</button>
        ${s.error ? `<button class="btn" data-act="retry">Retry failed (${s.error})</button>` : ""}
        <div class="dropdown">
          <button class="btn" data-act="export-menu">⤓ Export ▾</button>
          <div class="dropdown-menu" hidden>
            <a href="/api/channels/${encodeURIComponent(id)}/export?format=zip">ZIP of transcripts<small>Markdown + TXT + SRT per video, plus catalog.csv</small></a>
            <a href="/api/channels/${encodeURIComponent(id)}/export?format=json">JSON (everything)<small>Catalog and timestamped segments</small></a>
            <a href="/api/channels/${encodeURIComponent(id)}/export?format=csv">Catalog CSV<small>One row per video, no transcript text</small></a>
          </div>
        </div>
        <button class="icon-btn" data-act="delete" title="Remove channel">🗑</button>
      </div>
    </div>`;

  const tiles = `<div class="tiles">
      <div class="tile"><div class="label">Videos</div><div class="value">${fmtNum(s.total)}</div><div class="hint">${scanning ? "cataloguing…" : "in catalog"}</div></div>
      <div class="tile"><div class="label">Transcribed</div><div class="value" style="color:var(--done)">${fmtNum(s.done)}</div><div class="hint">${s.no_transcript ? `${s.no_transcript} without captions` : "&nbsp;"}</div></div>
      <div class="tile"><div class="label">Video length</div><div class="value">${fmtHours(s.seconds)}</div><div class="hint">total runtime</div></div>
      <div class="tile"><div class="label">Words</div><div class="value">${fmtNum(s.words)}</div><div class="hint">≈ ${fmtNum(Math.round(s.words / 300))} book pages</div></div>
    </div>`;

  const progress = `<div class="card">
      <div class="progress-head"><strong>${scanning ? "Finding every video on the channel…" :
        ch.status === "paused" ? "Paused" : ch.status === "error" ? "Stopped" : !s.total ? "No videos found" : finished >= s.total ? "All done" : "Transcribing"}</strong>
        <span class="pct">${scanning && !s.total ? "" : pct + "%"}</span></div>
      ${bar(s, scanning, "lg")}
      <div class="legend">${STATUS_ORDER.map(k => `<span><i class="dot ${k}"></i>${STATUS[k]} <b>${s[k] || 0}</b></span>`).join("")}</div>
      ${ch.status === "error" && ch.error ? `<div class="err-box"><b>Couldn't read this channel.</b> ${esc(ch.error)}</div>` : ""}
    </div>`;

  const chips = [["", "All", s.total], ...STATUS_ORDER.map(k => [k, STATUS[k], s[k] || 0])]
    .filter(([k, , n]) => !k || n)
    .map(([k, label, n]) => `<button class="chip ${f.status === k ? "on" : ""}" data-status="${k}">${k ? `<i class="dot ${k}"></i>` : ""}${label} <span class="n">${n}</span></button>`).join("");

  const toolbar = `<div class="toolbar">
      <div class="chips">${chips}</div>
      <span class="spacer"></span>
      <input type="search" id="title-filter" placeholder="Filter titles…" value="${esc(f.q)}">
      <select id="sort">${[["newest", "Newest"], ["oldest", "Oldest"], ["views", "Most viewed"], ["longest", "Longest"], ["title", "A → Z"]]
        .map(([v, l]) => `<option value="${v}" ${f.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <button class="btn small" data-act="layout">${f.layout === "grid" ? "☰ List" : "▦ Grid"}</button>
    </div>`;

  const cards = state.videos.map(v => `
    <div class="vcard" data-video="${esc(v.id)}">
      <div class="thumb">
        <img loading="lazy" src="${thumb(v.id)}" alt="" onerror="this.remove()">
        <span class="badge ${v.status}">${STATUS[v.status]}</span>
        ${v.kind !== "video" ? `<span class="kind">${esc(v.kind)}</span>` : ""}
        ${v.duration ? `<span class="dur">${fmtDur(v.duration)}</span>` : ""}
      </div>
      <div class="body">
        <div class="title">${esc(v.title)}</div>
        <div class="info">${[v.upload_date, v.view_count != null ? fmtNum(v.view_count) + " views" : "", v.word_count ? fmtNum(v.word_count) + " words" : ""]
          .filter(Boolean).map(esc).join(" · ")}</div>
      </div>
    </div>`).join("");

  const grid = state.videos.length ? `<div class="grid ${f.layout === "list" ? "list" : ""}">${cards}</div>`
    : `<div class="muted" style="padding:30px;text-align:center">${scanning ? "Videos will appear here as soon as the catalog is ready." : "No videos match."}</div>`;

  // Keep focus/caret in the title filter across re-renders.
  const focused = document.activeElement?.id === "title-filter";
  const caret = focused ? document.activeElement.selectionStart : 0;
  main.innerHTML = head + tiles + progress + toolbar + grid;
  if (focused) { const i = $("#title-filter"); i.focus(); i.setSelectionRange(caret, caret); }
  if (fresh) window.scrollTo(0, 0);
}

async function renderSearch() {
  const q = state.view.q;
  $("#search-q").value = q;
  main.innerHTML = `<h2>Results for “${esc(q)}”</h2><div class="muted">Searching…</div>`;
  const results = await api(`/api/search?q=${encodeURIComponent(q)}`);
  if (state.view.name !== "search" || state.view.q !== q) return;
  main.innerHTML = `<h2 style="margin-top:0">${results.length}${results.length === 100 ? "+" : ""} moments matching “${esc(q)}”</h2>
    <p class="muted" style="margin-top:-8px">Click a result to play the video right at that moment. Tip: put "quotes around a phrase".</p>
    <div class="results">${results.map(r => `
      <div class="result" data-video="${esc(r.video_id)}" data-t="${r.start}">
        <div class="rthumb"><img loading="lazy" src="${thumb(r.video_id)}" alt="" onerror="this.remove()"></div>
        <div><div class="title">${esc(r.title)}</div><div class="chan">${esc(r.channel_title || "")}</div>
        <div class="snip"><span class="ts">${fmtDur(r.start) || "0:00"}</span>${sanitizeSnippet(r.snippet)}</div></div>
      </div>`).join("") || `<div class="muted">No transcripts mention that yet.</div>`}</div>`;
}
// Snippets contain <mark> from SQLite; escape everything else.
function sanitizeSnippet(s) { return esc(s).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>"); }

let rendering = false;
async function render(fresh = false) {
  if (rendering && !fresh) return;
  rendering = true;
  try {
    renderSidebar();
    if (state.view.name === "channel") await renderChannel(fresh);
    else if (state.view.name === "search") { if (fresh) await renderSearch(); }
    else if (fresh || !main.querySelector(".hero")) renderHome();
  } catch (e) { console.error(e); } finally { rendering = false; }
}

// ---------- video drawer ----------
let playerTime = 0;
async function openVideo(id, t = 0) {
  const v = await api(`/api/videos/${encodeURIComponent(id)}`);
  state.video = v;
  const start = Math.floor(t);
  const segs = v.segments || [];
  $("#drawer-body").innerHTML = `
    <div class="player"><iframe id="yt" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen
      src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(v.id)}?enablejsapi=1&rel=0&start=${start}${t ? "&autoplay=1" : ""}"></iframe></div>
    <div class="vhead">
      <h2>${esc(v.title)}</h2>
      <div class="info">
        ${v.source ? `<span class="pill ${esc(v.source)}">${{ manual: "Human captions", auto: "Auto captions", whisper: "Whisper AI" }[v.source] || esc(v.source)}</span>` : ""}
        ${[v.upload_date, v.duration && fmtDur(v.duration), v.view_count != null && fmtNum(v.view_count) + " views", v.word_count && fmtNum(v.word_count) + " words", v.language]
          .filter(Boolean).map(esc).join(" · ")}
        · <a href="https://www.youtube.com/watch?v=${encodeURIComponent(v.id)}" target="_blank" rel="noopener">YouTube ↗</a>
      </div>
      ${segs.length ? `<div class="row">
        <input type="search" id="tx-filter" placeholder="Find in transcript…">
        <button class="btn small" data-act="copy">Copy text</button>
        <a class="btn small" href="/api/videos/${encodeURIComponent(v.id)}/download?format=md">.md</a>
        <a class="btn small" href="/api/videos/${encodeURIComponent(v.id)}/download?format=txt">.txt</a>
        <a class="btn small" href="/api/videos/${encodeURIComponent(v.id)}/download?format=srt">.srt</a>
      </div>` : ""}
    </div>
    <div class="transcript" id="transcript">${segs.length ? segs.map((s, i) =>
      `<div class="seg" data-i="${i}" data-t="${s.start}"><span class="ts">${fmtDur(s.start) || "0:00"}</span><span class="tx">${esc(s.text)}</span></div>`).join("")
      : `<div class="no-tx">${v.status === "no_transcript" ? "This video has no captions." + (v.error ? `<br><small>${esc(v.error)}</small>` : "")
        : v.status === "error" ? `Transcription failed: ${esc(v.error || "unknown error")}<br><br><button class="btn" data-act="retry-video">Try again</button>`
        : "Transcript not fetched yet — it's in the queue."}</div>`}</div>`;
  $("#drawer").hidden = false;
  document.body.style.overflow = "hidden";
  const iframe = $("#yt");
  iframe.addEventListener("load", () => iframe.contentWindow.postMessage(JSON.stringify({ event: "listening", id: 1 }), "*"));
  if (t) requestAnimationFrame(() => highlightAt(t, true));
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-body").innerHTML = "";
  document.body.style.overflow = "";
  state.video = null;
}

function seek(t) {
  const iframe = $("#yt");
  if (!iframe) return;
  const cmd = (func, args = []) => iframe.contentWindow.postMessage(JSON.stringify({ event: "command", func, args }), "*");
  cmd("seekTo", [t, true]); cmd("playVideo");
  highlightAt(t, false);
}

let lastIdx = -1;
function highlightAt(t, scroll) {
  const segs = state.video?.segments;
  if (!segs?.length) return;
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (segs[mid].start <= t) lo = mid; else hi = mid - 1; }
  if (lo === lastIdx && !scroll) return;
  $("#transcript .seg.now")?.classList.remove("now");
  const el = $(`#transcript .seg[data-i="${lo}"]`);
  if (!el) return;
  el.classList.add("now");
  lastIdx = lo;
  const box = $("#transcript");
  const r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
  if (scroll || r.top < b.top || r.bottom > b.bottom) el.scrollIntoView({ block: "center", behavior: scroll ? "auto" : "smooth" });
}

// The YouTube iframe reports its playback position; follow along in the transcript.
window.addEventListener("message", e => {
  if (!/youtube(-nocookie)?\.com$/.test(new URL(e.origin).hostname)) return;
  try {
    const d = typeof e.data === "string" ? JSON.parse(e.data) : e.data;
    const ct = d?.info?.currentTime;
    if (typeof ct === "number" && Math.abs(ct - playerTime) > 0.2) {
      playerTime = ct;
      if (!$("#tx-filter")?.value) highlightAt(ct, false);
    }
  } catch { /* not ours */ }
});

// ---------- events ----------
$("#add-form").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("#add-url").value;
  try {
    const r = await api("/api/channels", { method: "POST", body: JSON.stringify({
      url, include_shorts: $("#add-shorts").checked, include_streams: $("#add-streams").checked }) });
    $("#add-url").value = "";
    state.pendingUrl = r.url;
    toast("Got it — cataloguing the channel…");
    await refreshChannels();
    location.hash = `/c/${encodeURIComponent(r.id)}`;
  } catch (err) { toast(err.message); }
});

$("#search-form").addEventListener("submit", e => {
  e.preventDefault();
  const q = $("#search-q").value.trim();
  if (q) location.hash = `/search?q=${encodeURIComponent(q)}`;
});

$("#channel-list").addEventListener("click", e => {
  const it = e.target.closest("[data-ch]");
  if (it) { state.filter.status = ""; state.filter.q = ""; location.hash = `/c/${encodeURIComponent(it.dataset.ch)}`; }
});

document.addEventListener("click", async e => {
  const nav = e.target.closest("[data-nav=home]");
  if (nav) { e.preventDefault(); location.hash = ""; return; }

  if (e.target.closest("[data-close]")) { closeDrawer(); return; }

  const seg = e.target.closest(".seg");
  if (seg) { seek(+seg.dataset.t); return; }

  const vid = e.target.closest("[data-video]");
  if (vid) { openVideo(vid.dataset.video, +(vid.dataset.t || 0)); return; }

  const chip = e.target.closest("[data-status]");
  if (chip) { state.filter.status = chip.dataset.status; render(); return; }

  const menu = document.querySelector(".dropdown-menu");
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (menu && act !== "export-menu" && !e.target.closest(".dropdown-menu")) menu.hidden = true;
  if (!act) return;

  const id = state.view.id;
  const post = path => api(`/api/channels/${encodeURIComponent(id)}/${path}`, { method: "POST" });
  try {
    if (act === "export-menu") menu.hidden = !menu.hidden;
    else if (act === "pause") { await post("pause"); toast("Paused"); }
    else if (act === "resume") { await post("resume"); toast("Resumed"); }
    else if (act === "rescan") { await post("rescan"); toast("Looking for new videos…"); }
    else if (act === "retry") { const r = await post("retry"); toast(`Re-queued ${r.requeued} videos`); }
    else if (act === "layout") { state.filter.layout = state.filter.layout === "grid" ? "list" : "grid"; localSet("layout", state.filter.layout); }
    else if (act === "delete") {
      if (!confirm("Remove this channel and all its transcripts?")) return;
      await api(`/api/channels/${encodeURIComponent(id)}`, { method: "DELETE" });
      location.hash = ""; toast("Channel removed");
    } else if (act === "copy") {
      await navigator.clipboard.writeText(state.video.segments.map(s => s.text).join(" "));
      toast("Transcript copied");
    } else if (act === "retry-video") {
      await api(`/api/videos/${encodeURIComponent(state.video.id)}/retry`, { method: "POST" });
      closeDrawer(); toast("Queued again");
    }
    if (act !== "export-menu") { await refreshChannels(); render(); }
  } catch (err) { toast(err.message); }
});

let filterTimer;
document.addEventListener("input", e => {
  if (e.target.id === "title-filter") {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { state.filter.q = e.target.value; render(); }, 250);
  } else if (e.target.id === "tx-filter") {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#transcript .seg").forEach(el => {
      const tx = el.querySelector(".tx"), raw = state.video.segments[el.dataset.i].text;
      const hit = !q || raw.toLowerCase().includes(q);
      el.hidden = !hit;
      tx.innerHTML = q && hit ? esc(raw).replace(new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), m => `<mark>${m}</mark>`) : esc(raw);
    });
  }
});
document.addEventListener("change", e => {
  if (e.target.id === "sort") { state.filter.sort = e.target.value; render(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && state.video) closeDrawer(); });

// ---------- polling ----------
async function refreshChannels() {
  state.channels = await api("/api/channels");
  const st = await api("/api/status");
  const banner = $("#banner");
  const msgs = [];
  if (st.demo) msgs.push("Demo mode: channels and transcripts are generated locally, YouTube is not contacted.");
  if (st.blocked_reason) msgs.push(`⚠ ${st.blocked_reason} (${Math.ceil(st.blocked_for / 60)} min left)`);
  banner.hidden = !msgs.length;
  banner.textContent = msgs.join("  ");
}

async function tick() {
  try {
    await refreshChannels();
    const busy = state.channels.some(c => c.status === "scanning" || (c.status === "active" && (c.stats.pending || c.stats.working)));
    // Don't redraw under an open dropdown.
    if (!document.querySelector(".dropdown-menu:not([hidden])")) await render();
    setTimeout(tick, busy ? 2000 : 6000);
  } catch { setTimeout(tick, 5000); }
}

refreshChannels().then(route).finally(() => setTimeout(tick, 2000));
