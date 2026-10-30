/**
 * background.js — 抓音轨 URL → 调本地引擎 → 轮询 → blob URL 回传页面
 *
 * 音轨捕获：webRequest 观测该标签页的媒体请求（复用 collect.js 的判定：
 * 媒体扩展名 + audio 命中词优先），点按钮后观察 5 秒，期间视频需在播放。
 * 全程走 service worker fetch（host_permissions 豁免 CORS），成品以
 * blob: URL 交给页面，绕开 https 页面访问 http://127.0.0.1 的混合内容限制。
 */

const SERVER = "http://127.0.0.1:8765";
const MEDIA_RE = /\.(m4s|mp4|m4a|aac|webm|mp3|ogg|wav|flac)(\?|#|$)/i;
const PLAYLIST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
const AUDIO_HINT = /(audio|aud|aac|m4a|dash_audio|_140|_251|30280|30216|30232)/i;

let cap = null; // {tabId, urls: [], timer}

chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    if (cap && d.tabId === cap.tabId && !PLAYLIST_RE.test(d.url)) {
      cap.urls.push(d.url);
    }
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] },
  []
);

function pickAudio(urls) {
  const uniq = [...new Set(urls)];
  const audio = uniq.filter((u) => AUDIO_HINT.test(u) && MEDIA_RE.test(u));
  const media = uniq.filter((u) => MEDIA_RE.test(u));
  return audio[0] || media[0] || null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function toTab(tabId, msg) {
  try {
    await chrome.tabs.sendMessage(tabId, msg);
  } catch (_) {
    /* 页面还没准备好就忽略 */
  }
}

async function api(path, opts) {
  const r = await fetch(SERVER + path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

async function dubStart(tabId, pageUrl, lang) {
  cap = { tabId, urls: [], timer: null };
  await toTab(tabId, { type: "dub-status", stage: "捕音轨", detail: "播放视频 2 秒" });
  await sleep(5000);
  const url = pickAudio(cap.urls);
  const found = cap.urls.length;
  cap = null;
  if (!url) {
    await toTab(tabId, {
      type: "dub-status",
      stage: "error",
      detail: `未捕获音轨（观察到 ${found} 个请求）——先播放视频再点一次`,
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
