/**
 * content.js — 纯 content script 架构（v0.2，无 background/SW）
 *
 * 为什么没有 SW：MV3 service worker 闲置 30s 被杀，消息链/长驻轮询全部
 * 不可靠（v0.1 三次真机翻车的根因）。本页面自己就是司机：
 *   音轨候选 = performance 资源历史（含 transferSize）+ video src + YT 特例
 *   请求     = 直接 fetch http://127.0.0.1:8765（CORS+PNA 由引擎响应头解决）
 *   播放     = blob URL + Audio 元素随 <video> 同步
 */
(() => {
  if (window.__dubInstalled) return;
  window.__dubInstalled = true;

  // 页面资源历史默认只留 ~250 条，长页面会挤掉音轨请求，扩容
  try { performance.setResourceTimingBufferSize(4000); } catch (_) {}

  const SERVER = "http://127.0.0.1:8765";
  const MEDIA_RE = /\.(m4s|mp4|m4a|aac|webm|mp3|ogg|wav|flac)(\?|#|$)/i;
  const PLAYLIST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
  const AUDIO_HINT = /(audio|aud|aac|m4a|mp3|wav|ogg|flac|dash_audio|_140|_251|30280|30216|30232)/i;
  const STAGE_ZH = {
    extract: "抽音轨", separate: "Demucs 分离", asr: "Whisper 识别",
    translate: "LLM 翻译", tts: "配音合成", assemble: "混音", encode: "转码",
  };

  // ------------------------------------------------------------------ UI

  const pill = document.createElement("div");
  Object.assign(pill.style, {
    position: "fixed", top: "12px", right: "12px", zIndex: 2147483647,
    padding: "8px 14px", borderRadius: "999px", font: "13px/1.4 system-ui",
    color: "#fff", background: "rgba(20,20,24,.82)", boxShadow: "0 2px 10px rgba(0,0,0,.35)",
    display: "none", cursor: "default", backdropFilter: "blur(6px)",
  });
  document.documentElement.appendChild(pill);

  function show(html, ms) {
    pill.innerHTML = html;
    pill.style.display = "block";
    if (ms) setTimeout(() => (pill.style.display = "none"), ms);
  }

  const btn = document.createElement("button");
  btn.textContent = "🎙 AI 配音";
  Object.assign(btn.style, {
    position: "fixed", top: "12px", right: "12px", zIndex: 2147483647,
    padding: "8px 16px", borderRadius: "999px", border: "0", cursor: "pointer",
    font: "600 13px/1.4 system-ui", color: "#04140c",
    background: "linear-gradient(135deg,#42e695,#3bb2b8)",
    boxShadow: "0 2px 10px rgba(0,0,0,.35)",
  });
  btn.onclick = () => begin().catch((e) => fail("启动失败：" + e.message));
  document.documentElement.appendChild(btn);

  function fail(msg) {
    if (timer) clearInterval(timer);
    show("❌ " + msg, 15000);
    btn.style.display = "block";
  }

  // ------------------------------------------------------------- 音轨挑选

  function sniff() {
    const out = [];
    for (const e of performance.getEntriesByType("resource")) {
      const u = e.name;
      if (!/^https?:/.test(u) || PLAYLIST_RE.test(u) || !MEDIA_RE.test(u)) continue;
      out.push({ url: u, size: e.transferSize || 0, forced: false });
    }
    document.querySelectorAll("video,audio,source").forEach((el) => {
      for (const u of [el.src, el.currentSrc]) {
        if (u && /^https?:/.test(u) && MEDIA_RE.test(u) && !PLAYLIST_RE.test(u)) {
          out.push({ url: u, size: 0, forced: false });
        }
      }
    });
    try {
      const pr = window.ytInitialPlayerResponse;
      (pr?.streamingData?.adaptiveFormats || [])
        .filter((f) => (f.mimeType || "").startsWith("audio/") && f.url)
        .forEach((f) => out.push({ url: f.url, size: Number(f.contentLength) || 0, forced: true }));
    } catch (_) { /* 非 YouTube */ }
    return out;
  }

  function pick() {
    const ok = sniff()
      .map((c) => {
        let s = 0;
        if (c.forced) s += 10;
        if (AUDIO_HINT.test(c.url)) s += 2;
        if (/\.(m4a|aac|mp3|flac|ogg|wav)(\?|#|$)/i.test(c.url)) s += 1;
        if (/\.m4s(\?|#|$)/i.test(c.url)) s += 1;
        if (c.size > 300 * 1024) s += 2;
        return { ...c, s };
      })
      // 资格线：必须有音频特征（video m4s 只有 m4s+体积，混不进来）
      .filter((c) => c.s >= 3 && (c.forced || AUDIO_HINT.test(c.url)))
      .sort((a, b) => b.s - a.s);
    return ok[0] || null;
  }

  // ---------------------------------------------------------------- 流程

  let timer = null;
  let jobId = null;

  async function xfetch(url, opts = {}, timeoutMs = 15000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await fetch(url, { ...opts, signal: ctrl.signal });
    } finally {
      clearTimeout(t);
    }
  }

  async function begin() {
    btn.style.display = "none";
    show("读页面记录…");
    const c = pick();
    if (!c) {
      fail("没找到像音轨的请求——重新打开视频页、播放 2 秒再试（按 F12 → Network 应能看到 .m4s）");
      return;
    }
    console.log("[dub] 音轨:", c.url, "score", c.s);
    show("⏳ 排队…");
    let job;
    try {
      const r = await xfetch(SERVER + "/api/dub", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: c.url, referer: location.href, lang: "zh" }),
      });
      job = await r.json();
    } catch (e) {
      fail("连不上本地引擎（python -m dub serve）：" + e.message);
      return;
    }
    if (!job.job_id) {
      fail(job.error || "提交失败");
      return;
    }
    jobId = job.job_id;
    show("⏳ 排队…");
    timer = setInterval(poll, 3000);
    poll();
  }

  async function poll() {
    let st;
    try {
      const r = await xfetch(`${SERVER}/api/job/${jobId}`);
      st = await r.json();
    } catch (_) {
      return; // 引擎忙/瞬时失联，下轮再试
    }
    if (st.status === "error") { fail(st.error || "引擎出错"); return; }
    if (st.status === "done") {
      clearInterval(timer);
      show("⏳ 取回音频…");
      try {
        const r = await xfetch(`${SERVER}/api/job/${jobId}/audio`, {}, 60000);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const blob = await r.blob();
        attach(URL.createObjectURL(blob));
      } catch (e) {
        fail("取音频失败：" + e.message);
      }
      return;
    }
    show(`⏳ ${STAGE_ZH[st.stage] || st.stage || "处理中"}`);
  }

  // ---------------------------------------------------------------- 播放

  function attach(url) {
    const vids = [...document.querySelectorAll("video")].filter((v) => v.duration > 0 || v.src || v.currentSrc);
    const video = vids.sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0];
    if (!video) { show("页面上没找到 <video>", 6000); btn.style.display = "block"; return; }
    video.muted = true;
    const audio = new Audio(url);
    audio.preload = "auto";
    sync(video, audio);
    if (!video.paused) { audio.currentTime = video.currentTime; audio.play().catch(() => {}); }
    // 成功提示常驻（视频暂停时也要让人看见）
    show("🔊 AI 配音已就绪，原声已静音" + (video.paused ? " —— 点播放开始" : ""));
    btn.textContent = "🎙 重新配音";
    btn.style.display = "block";
  }

  function sync(video, audio) {
    const follow = () => {
      if (!video.paused && Math.abs(audio.currentTime - video.currentTime) > 0.35) {
        audio.currentTime = video.currentTime;
      }
    };
    video.addEventListener("play", () => { audio.currentTime = video.currentTime; audio.play(); });
    video.addEventListener("pause", () => audio.pause());
    video.addEventListener("seeked", () => { audio.currentTime = video.currentTime; });
    video.addEventListener("ratechange", () => { audio.playbackRate = video.playbackRate; });
    video.addEventListener("timeupdate", follow);
  }
})();
