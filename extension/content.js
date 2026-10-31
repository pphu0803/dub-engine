/**
 * content.js — 纯 content script 架构（v0.4，无 background/SW）+ 分段流式播放
 *
 * 播放器是双 Audio 元素乒乓：当前段播放时预载下一段，边界切换在已就绪的
 * 元素上瞬时完成（v0.3 单元素换 src 会在加载窗口里丢播放——13 段攒出
 * "配音比原片短"和"点播放没声音"两个症状的根因）。
 * 时长对齐：loadedmetadata 后按 窗口/实际时长 微调 playbackRate（0.97~1.05）。
 * seek 到未完成段自动回退原声，就绪后切回配音。
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
    position: "fixed", top: "58px", right: "12px", zIndex: 2147483647, // pill 下方，不遮挡进度
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

  // ---------------------------------------------------------------- 播放器

  const player = {
    started: false, video: null, chunks: [], cur: -1, align: 1, curEl: 0,
    elems: [new Audio(), new Audio()],
  };
  const el = () => player.elems[player.curEl];
  const other = () => player.elems[1 - player.curEl];
  const chunkUrl = (k) => `${SERVER}/api/job/${jobId}/audio/${k}`;

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

    const chunks = (st.chunks || []).map((c) => ({ ...c, url: chunkUrl(c.i) }));
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
      }
      return;
    }
    if (st.status === "running") {
      if (!chunks.length) {
        show(`⏳ ${st.stage === "extract" ? "抽取整轨" : st.stage || "处理中"}…`);
      } else {
        show(`⏳ 配音中 ${doneN}/${chunks.length} 段`);
        if (player.started) syncNow(); // 新段就绪后，若正停在未完成段上则切回配音
      }
      if (!player.started && chunks[0] && chunks[0].status === "done") {
        startPlayer();
      }
    }
  }

  function startPlayer() {
    const vids = [...document.querySelectorAll("video")].filter((v) => v.duration > 0 || v.src || v.currentSrc);
    const video = vids.sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0];
    if (!video) { show("页面上没找到 <video>", 6000); btn.style.display = "block"; return; }
    player.started = true;
    player.video = video;
    video.muted = true;
    bind(video);
    show("🔊 AI 配音播放中（分段流式；未完成段自动回退原声）");
    btn.textContent = "🎙 重新配音";
    btn.style.display = "block";
    syncNow(true);
    ensurePlaying();
  }

  let bound = false;

  function bind(video) {
    if (bound) return;
    bound = true;
    video.addEventListener("play", () => { syncNow(true); ensurePlaying(); });
    video.addEventListener("pause", () => el().pause());
    video.addEventListener("seeked", () => { syncNow(true); ensurePlaying(); });
    video.addEventListener("ratechange", () => { el().playbackRate = video.playbackRate * player.align; });
    video.addEventListener("timeupdate", () => syncNow(false));
  }

  function ensurePlaying() {
    const a = el();
    const go = () => {
      if (player.video && !player.video.paused && a.paused) {
        a.play().catch((e) => show("❌ 配音播放受阻：" + e.message, 6000));
      }
    };
    if (a.readyState >= 2) go();
    else a.addEventListener("canplaythrough", go, { once: true });
  }

  function chunkAt(t) {
    return player.chunks.find((c) => t >= c.start - 0.05 && t < c.end + 0.05);
  }

  function applyAlign(a, c) {
    if (a.duration && isFinite(a.duration)) {
      const w = c.end - c.start;
      const r = Math.min(1.05, Math.max(0.97, w / a.duration));
      player.align = Math.abs(r - 1) > 0.004 ? r : 1;
      a.playbackRate = player.video.playbackRate * player.align;
    }
  }

  function syncNow(force) {
    if (!player.started || !player.video) return;
    const video = player.video;
    const t = video.currentTime;
    const c = chunkAt(t);
    if (!c) return;
    if (c.status !== "done") {
      // 未完成段：退回原声，等后台出活（poll 里新段就绪会再触发 syncNow）
      el().pause();
      player.cur = -1;
      video.muted = false;
      show(`⏳ 第 ${c.i + 1} 段处理中——此处先听原声`);
      return;
    }

    const needSwitch = player.cur !== c.i;
    if (needSwitch || force) {
      const off = Math.max(0, t - c.start);
      const nEl = other();
      const preloaded = nEl.src.endsWith(`/audio/${c.i}`) && nEl.readyState >= 2;
      if (needSwitch && preloaded) {
        // 乒乓切换：预载好的元素瞬时接管，无加载缺口
        el().pause();
        nEl.playbackRate = video.playbackRate * player.align;
        nEl.currentTime = off;
        if (!video.paused) nEl.play().catch((e) => show("❌ 换段播放失败：" + e.message, 6000));
        player.curEl = 1 - player.curEl;
      } else {
        const a = el();
        if (needSwitch || !a.src.includes(`/audio/${c.i}`)) {
          a.src = chunkUrl(c.i);
          a.addEventListener("loadedmetadata", () => applyAlign(a, c), { once: true });
        }
        a.playbackRate = video.playbackRate * player.align;
        const go = () => {
          if (player.cur !== c.i && !force) return;
          a.currentTime = off;
          if (!video.paused) a.play().catch((e) => show("❌ 配音播放受阻：" + e.message, 6000));
        };
        if (a.readyState >= 2) go();
        else a.addEventListener("canplaythrough", go, { once: true });
      }
      player.cur = c.i;
      video.muted = true;
      // 预载下一段到另一个元素
      const nxt = player.chunks.find((x) => x.i === c.i + 1);
      if (nxt && nxt.status === "done") {
        const o = other();
        if (!o.src.endsWith(`/audio/${nxt.i}`)) o.src = nxt.url;
      }
    } else if (!video.paused) {
      const a = el();
      const off = t - c.start;
      if (a.paused) {
        a.currentTime = Math.max(0, off);
        a.play().catch(() => {});
      } else if (Math.abs(a.currentTime - off) > 0.35) {
        a.currentTime = Math.max(0, off);
      }
    }
  }
})();
