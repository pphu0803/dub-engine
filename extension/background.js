/**
 * background.js — 音轨候选收集 → 打分挑选 → 调本地引擎 → blob URL 回传
 *
 * 候选两个来源（SW 会被 MV3 杀掉重启，所以记录必须放 storage.session）：
 *   1. content script 读 performance 资源历史（主来源，页面自己记全账）
 *   2. webRequest 观测（带 content-type/大小，用于打分加权）
 * 打分：audio/* content-type、B 站音轨特征码、音频扩展名、m4s、体积；
 * HTML/JSON/图片强惩罚。低于阈值直接报"没找到"，不再瞎抓。
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

function pick(candidates, tabId) {
  const now = Date.now();
  for (const u of candidates || []) note(tabId, u, "", 0); // sniff 结果并入
  const all = Object.entries(media[tabId] || {})
    .map(([url, e]) => ({ url, ...e, s: score(url, e) }));
  const fresh = all.filter((e) => now - e.ts <= 6000);
  const best = (pool) => pool.slice().sort((a, b) => b.s - a.s)[0];
  const bf = best(fresh), ba = best(all);
  if (bf && bf.s >= 3) return bf.url;
  if (ba && ba.s >= 3) return ba.url;
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function toTab(tabId, msg) {
  try {
    await chrome.tabs.sendMessage(tabId, msg);
  } catch (_) {
    return null;
  }
}

async function api(path, opts) {
  const r = await fetch(SERVER + path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

async function dubStart(tabId, pageUrl, lang) {
  await toTab(tabId, { type: "dub-status", stage: "读页面记录", detail: "" });
  const sniffed = await toTab(tabId, { type: "dub-sniff" });
  const url = pick((sniffed && sniffed.urls) || [], tabId);
  if (!url) {
    await toTab(tabId, {
      type: "dub-status", stage: "error",
      detail: "没找到像音轨的请求——确认扩展已刷新后，重新打开视频页播放 2 秒再试",
    });
    return;
  }
  console.log("[dub] 音轨:", url);

  let job;
  try {
    await toTab(tabId, { type: "dub-status", stage: "排队", detail: "提交本地引擎" });
    job = await api("/api/dub", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, referer: pageUrl, lang: lang || "zh" }),
    });
  } catch (e) {
    await toTab(tabId, { type: "dub-status", stage: "error", detail: "连不上本地引擎：python -m dub serve" });
    return;
  }

  const STAGE_ZH = {
    extract: "抽音轨", separate: "Demucs 分离", asr: "Whisper 识别",
    translate: "LLM 翻译", tts: "配音合成", assemble: "混音", encode: "转码",
  };
  for (;;) {
    await sleep(2000);
    let st;
    try {
      st = await api(`/api/job/${job.job_id}`);
    } catch (_) {
      continue;
    }
    if (st.status === "error") {
      await toTab(tabId, { type: "dub-status", stage: "error", detail: st.error || "引擎出错" });
      return;
    }
    if (st.status === "done") {
      await toTab(tabId, { type: "dub-status", stage: "取回音频" });
      const r = await fetch(`${SERVER}/api/job/${job.job_id}/audio`);
      const blob = await r.blob();
      const blobUrl = URL.createObjectURL(blob);
      await toTab(tabId, {
        type: "dub-ready",
        url: blobUrl,
        segments: st.segments || [],
        timings: st.timings || {},
      });
      return;
    }
    await toTab(tabId, { type: "dub-status", stage: STAGE_ZH[st.stage] || st.stage });
  }
}

chrome.runtime.onMessage.addListener((m, sender, reply) => {
  if (m.type === "dub-start" && sender.tab) {
    dubStart(sender.tab.id, sender.tab.url, m.lang);
    reply({ ok: true });
  }
  return false;
});
