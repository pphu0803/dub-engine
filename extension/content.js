/**
 * content.js — 纯 content script 架构（v0.3，无 background/SW）+ 分段流式播放
 *
 * 引擎按 ~60s 静音点切段顺序处理：段 0 完成即开始播放，后面段落边播边出。
 * 播放器跟随 <video>：play/pause/seek/倍速全同步；seek 到未完成段自动
 * 回退原声（不静音），该段就绪后自动切回配音。
 * 音轨候选 = performance 资源历史 + video src + YT 特例（videoplayback/itag）。
 */
(() => {
  if (window.__dubInstalled) return;
  window.__dubInstalled = true;

  // 页面资源历史默认只留 ~250 条，长页面会挤掉音轨请求，扩容
  try { performance.setResourceTimingBufferSize(4000); } catch (_) {}

  const SERVER = "http://127.0.0.1:8765";
  const MEDIA_RE = /\.(m4s|mp4|m4a|aac|webm|mp3|ogg|wav|flac)(\?|#|$)/i;
  const STREAM_RE = /videoplayback\?/i;
  const PLAYLIST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
  const AUDIO_HINT = /(audio|aud|aac|m4a|mp3|wav|ogg|flac|dash_audio|_140|_251|30280|30216|30232|itag=140|itag=139|itag=141|itag=599|itag=600)/i;

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
    const isMedia = (u) => MEDIA_RE.test(u) || STREAM_RE.test(u);
    const out = [];
    for (const e of performance.getEntriesByType("resource")) {
      const u = e.name;
      if (!/^https?:/.test(u) || PLAYLIST_RE.test(u) || !isMedia(u)) continue;
      out.push({ url: u, size: e.transferSize || 0, forced: false });
    }
    document.querySelectorAll("video,audio,source").forEach((el) => {
      for (const u of [el.src, el.currentSrc]) {
        if (u && /^https?:/.test(u) && isMedia(u) && !PLAYLIST_RE.test(u)) {
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
        if (/itag=(140|139|141|599|600)\b/i.test(c.url)) s += 2;
        if (/\.(m4a|aac|mp3|flac|ogg|wav)(\?|#|$)/i.test(c.url)) s += 1;
        if (/\.m4s(\?|#|$)/i.test(c.url)) s += 1;
        if (c.size > 300 * 1024) s += 2;
        return { ...c, s };
      })
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
      fail("没找到像音轨的请求——重新打开视频页、播放 2 秒再试");
      return;
    }
    console.log("[dub] 音轨:", c.url, "score", c.s);
    show("⏳ 提交引擎…");
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

  const player = { started: false, video: null, chunks: [], cur: -1, audio: new Audio() };

  async function poll() {
    let st;
    try {
      const r = await xfetch(`${SERVER}/api/job/${jobId}`);
      st = await r.json();
    } catch (_) {
      return; // 引擎忙/瞬时失联，下轮再试
    }
    if (st.error && !st.status) { fail(st.error); return; }
    if (st.status === "error") { fail(st.error || "引擎出错"); return; }

    const chunks = st.chunks || [];
    player.chunks = chunks;
    const doneN = chunks.filter((c) => c.status === "done").length;

    if (st.status === "done") {
      clearInterval(timer);
      if (player.started) {
        show(`✅ 全部 ${chunks.length} 段完成（用时 ${st.total_s || "?"}s）`, 8000);
        btn.textContent = "🎙 重新配音";
        btn.style.display = "block";
      } else if (chunks.length) {
        startPlayer();
        return;
      }
      return;
    }
    if (st.status === "running") {
      if (!chunks.length) {
        show(`⏳ ${st.stage === "extract" ? "抽取整轨" : st.stage || "处理中"}…`);
      } else {
        show(`⏳ 配音中 ${doneN}/${chunks.length} 段`);
      }
      if (!player.started && chunks[0] && chunks[0].status === "done") {
        startPlayer();
        return;
      }
    }
  }

  // ---------------------------------------------------------------- 播放

  function startPlayer() {
    const vids = [...document.querySelectorAll("video")].filter((v) => v.duration > 0 || v.src || v.currentSrc);
    const video = vids.sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0];
    if (!video) { show("页面上没找到 <video>", 6000); btn.style.display = "block"; return; }
    player.started = true;
    player.video = video;
    video.muted = true;
    bind(video);
    if (!video.paused) player.audio.play().catch(() => {});
    show("🔊 AI 配音播放中（分段流式；未完成段自动回退原声）");
    btn.textContent = "🎙 重新配音";
    btn.style.display = "block";
    syncNow();
  }

  let bound = false;

  function bind(video) {
    if (bound) return;
    bound = true;
    video.addEventListener("play", () => { syncNow(); player.audio.play().catch(() => {}); });
    video.addEventListener("pause", () => player.audio.pause());
    video.addEventListener("seeked", () => syncNow());
    video.addEventListener("ratechange", () => { player.audio.playbackRate = video.playbackRate; });
    video.addEventListener("timeupdate", () => syncNow());
  }

  function chunkAt(t) {
    return player.chunks.find((c) => t >= c.start - 0.05 && t < c.end + 0.05);
  }

  function syncNow() {
    if (!player.started || !player.video) return;
    const video = player.video;
    const t = video.currentTime;
    const c = chunkAt(t);
    if (!c) return;
    if (c.status !== "done") {
      // 未完成段：退回原声，等后台出活
      player.audio.pause();
      player.cur = -1;
      video.muted = false;
      show(`⏳ 第 ${c.i + 1} 段处理中——此处先听原声`);
      return;
    }
    if (player.cur !== c.i) {
      player.cur = c.i;
      player.audio.src = `${SERVER}/api/job/${jobId}/audio/${c.i}`;
      player.audio.currentTime = Math.max(0, t - c.start);
      if (!video.paused) player.audio.play().catch(() => {});
    } else if (!video.paused && Math.abs(player.audio.currentTime - (t - c.start)) > 0.35) {
      player.audio.currentTime = t - c.start;
    }
    video.muted = true;
  }
})();
