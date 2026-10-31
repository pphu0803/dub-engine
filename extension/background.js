/**
 * background.js — 无状态代理（MV3 生命周期友好）
 *
 * SW 随时可能被杀，所以：长驻逻辑（轮询/状态机）全在 content script，
 * 这里只做三类单发动作：dub-begin（挑音轨+提交）、job-status（查一次）、
 * job-audio（取一次，base64 回传）。每次消息唤醒 SW，干完即睡。
 * 音轨候选：content 送来 performance 历史 + webRequest 观测记录
 * （storage.session 持久化）合并打分。
 */

const SERVER = "http://127.0.0.1:8765";
const MEDIA_RE = /\.(m4s|mp4|m4a|aac|webm|mp3|ogg|wav|flac)(\?|#|$)/i;
const PLAYLIST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
const AUDIO_HINT = /(audio|aud|aac|m4a|dash_audio|_140|_251|30280|30216|30232)/i;

let media = {}; // {tabId: {url: {ct, len, ts}}}，与 storage.session 同步
let flushTimer = null;

function note(tabId, url, ct, len) {
  if (PLAYLIST_RE.test(url) || !MEDIA_RE.test(url)) return;
  const t = (media[tabId] = media[tabId] || {});
  const prev = t[url];
  t[url] = {
    ct: ct || (prev && prev.ct) || "",
    len: len || (prev && prev.len) || 0,
    ts: (prev && prev.ts) || Date.now(),
  };
  scheduleFlush();
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setTimeout(async () => {
    flushTimer = null;
    try { await chrome.storage.session.set({ media }); } catch (_) {}
  }, 800);
}

chrome.storage.session.get("media").then((o) => { if (o.media) media = o.media; }).catch(() => {});

chrome.webRequest.onBeforeRequest.addListener(
  (d) => note(d.tabId, d.url, "", 0),
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] },
  []
);

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    const h = {};
    for (const x of d.responseHeaders || []) h[x.name.toLowerCase()] = x.value;
    note(d.tabId, d.url, h["content-type"], parseInt(h["content-length"]) || 0);
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] },
  ["responseHeaders"]
);

function score(url, e) {
  let s = 0;
  const ct = (e.ct || "").toLowerCase();
  if (ct.startsWith("audio/")) s += 3;
  if (AUDIO_HINT.test(url)) s += 2;
  if (/\.(m4a|aac|mp3|flac|ogg|wav)(\?|#|$)/i.test(url)) s += 1;
  if (/\.m4s(\?|#|$)/i.test(url)) s += 1;
  if (e.len > 300 * 1024) s += 2;
  if (e.len && e.len < 100 * 1024) s -= 5;
  if (/text\/html|application\/json|javascript|image\//.test(ct)) s -= 10;
  return s;
}

function pick(urls, tabId) {
  for (const u of urls || []) note(tabId, u, "", 0);
  const now = Date.now();
  const all = Object.entries(media[tabId] || {})
    .map(([url, e]) => ({ url, ...e, s: score(url, e) }));
  const fresh = all.filter((e) => now - e.ts <= 6000);
  const best = (pool) => pool.slice().sort((a, b) => b.s - a.s)[0];
  const bf = best(fresh), ba = best(all);
  if (bf && bf.s >= 3) return bf.url;
  if (ba && ba.s >= 3) return ba.url;
  return null;
}

async function api(path, opts) {
  const r = await fetch(SERVER + path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

chrome.runtime.onMessage.addListener((m, sender, rep) => {
  (async () => {
    try {
      if (m.type === "dub-begin" && sender.tab) {
        const url = pick(m.urls || [], sender.tab.id);
        if (!url) {
          rep({ error: "没找到像音轨的请求——重新打开视频页、播放 2 秒再试" });
          return;
        }
        console.log("[dub] 音轨:", url);
        const job = await api("/api/dub", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, referer: m.pageUrl, lang: m.lang || "zh" }),
        });
        rep({ jobId: job.job_id });
        return;
      }
      if (m.type === "job-status") {
        rep(await api(`/api/job/${m.jobId}`));
        return;
      }
      if (m.type === "job-audio") {
        const r = await fetch(`${SERVER}/api/job/${m.jobId}/audio`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const buf = new Uint8Array(await r.arrayBuffer());
        let bin = "";
        for (let i = 0; i < buf.length; i += 0x8000) {
          bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        }
        rep({ b64: btoa(bin) });
        return;
      }
    } catch (e) {
      rep({ error: String((e && e.message) || e) });
      return;
    }
  })();
  return true; // 异步回复
});
