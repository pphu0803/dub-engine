/**
 * background.js — 观测媒体请求 → 按分挑选音轨 → 调本地引擎 → blob URL 回传
 *
 * 音轨捕获不是"5 秒窗口碰运气"：持续记录每个标签页观察到的媒体 URL
 * （content-type / 大小 / 时间），点按钮后优先用最近窗口里的最佳候选，
 * 窗口里没有好货就回退到本标签页近 15 分钟内的历史最佳——真音轨在页面
 * 加载时只出现一次，重试靠的就是这份历史。
 */

const SERVER = "http://127.0.0.1:8765";
const MEDIA_RE = /\.(m4s|mp4|m4a|aac|webm|mp3|ogg|wav|flac)(\?|#|$)/i;
const PLAYLIST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
const AUDIO_HINT = /(audio|aud|aac|m4a|dash_audio|_140|_251|30280|30216|30232)/i;

const KEEP_MS = 15 * 60 * 1000;
const tabMedia = new Map(); // tabId -> Map(url -> {ct, len, ts})

function noteMedia(tabId, url, ct, len) {
  if (PLAYLIST_RE.test(url) || !MEDIA_RE.test(url)) return;
  if (!tabMedia.has(tabId)) tabMedia.set(tabId, new Map());
  const m = tabMedia.get(tabId);
  const prev = m.get(url);
  m.set(url, {
    ct: ct || (prev && prev.ct) || "",
    len: len || (prev && prev.len) || 0,
    ts: (prev && prev.ts) || Date.now(),
  });
}

chrome.webRequest.onBeforeRequest.addListener(
  (d) => noteMedia(d.tabId, d.url, "", 0),
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] },
  []
);

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    const h = {};
    for (const x of d.responseHeaders || []) h[x.name.toLowerCase()] = x.value;
    noteMedia(d.tabId, d.url, h["content-type"], parseInt(h["content-length"]) || 0);
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
  if (e.len > 300 * 1024) s += 2;
  if (e.len && e.len < 100 * 1024) s -= 5;
  if (/text\/html|application\/json|javascript|image\//.test(ct)) s -= 10;
  return s;
}

function pick(tabId) {
  const all = [...(tabMedia.get(tabId) || new Map()).entries()]
    .map(([url, e]) => ({ url, ...e, s: score(url, e) }));
  const fresh = all.filter((e) => Date.now() - e.ts <= 6000);
  const best = (pool) => pool.slice().sort((a, b) => b.s - a.s)[0];
  const bf = best(fresh), ba = best(all);
  if (bf && bf.s >= 3) return { url: bf.url, src: "窗口" };
  if (ba && ba.s >= 3) return { url: ba.url, src: "历史" };
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function toTab(tabId, msg) {
  try {
    await chrome.tabs.sendMessage(tabId, msg);
  } catch (_) {
    /* 页面未就绪忽略 */
  }
}

async function api(path, opts) {
  const r = await fetch(SERVER + path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

async function dubStart(tabId, pageUrl, lang) {
  // 清理过期历史
  for (const [t, m] of tabMedia) {
    for (const [u, e] of m) if (Date.now() - e.ts > KEEP_MS) m.delete(u);
    if (!m.size) tabMedia.delete(t);
  }
  await toTab(tabId, { type: "dub-status", stage: "选音轨", detail: "从页面观察记录里挑" });
  await sleep(3000);
  const hit = pick(tabId);
  if (!hit) {
    await toTab(tabId, {
      type: "dub-status", stage: "error",
      detail: "没找到像音轨的请求——刷新视频页、播放 2 秒再点一次",
    });
    return;
  }
  console.log(`[dub] 音轨(${hit.src}):`, hit.url);

  let job;
  try {
    await toTab(tabId, { type: "dub-status", stage: "排队", detail: "提交本地引擎" });
    job = await api("/api/dub", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: hit.url, referer: pageUrl, lang: lang || "zh" }),
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
