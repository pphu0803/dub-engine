/**
 * content.js — 纯 content script 架构（v0.5，无 background/SW）+ 分段流式播放
 *
 * 播放器：双 Audio 元素乒乓预载。关键约束：**媒体元素不能直连
 * http://127.0.0.1**（PNA 对 no-cors 媒体子资源会拦，error 4），必须
 * fetch → blob → objectURL（fetch 有 CORS+PNA 响应头放行，实测可通）。
 * 引擎按 ~60s 静音点切段顺序处理：段 0 完成即开始播放，边播边出。
 * seek 到未完成段自动回退原声，就绪后切回配音。
 */
(() => {
  if (window.__dubInstalled) return;
  window.__dubInstalled = true;

  // 页面资源历史默认只留 ~250 条，长页面会挤掉音轨请求，扩容
  try { performance.setResourceTimingBufferSize(4000); } catch (_) {}

  const SERVER = "http://127.0.0.1:8765";
  // 扩展名判定只看路径（? 之前），防信标/日志 URL 靠查询串混进来
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

  const tgl = document.createElement("button");
  tgl.textContent = "🔈 切回原声";
  Object.assign(tgl.style, {
    position: "fixed", top: "104px", right: "12px", zIndex: 2147483647,
    padding: "8px 16px", borderRadius: "999px", border: "0", cursor: "pointer",
    font: "600 13px/1.4 system-ui", color: "#0c1a2e",
    background: "linear-gradient(135deg,#8ec5fc,#e0c3fc)",
    boxShadow: "0 2px 10px rgba(0,0,0,.35)", display: "none",
  });
  tgl.onclick = () => {
    if (!player.started) return;
    player.mode = player.mode === "dub" ? "orig" : "dub";
    tgl.textContent = player.mode === "dub" ? "🔈 切回原声" : "🔊 切回配音";
    if (player.mode === "orig") {
      el().pause();
      player.video.muted = false;
      show("🔈 原声模式（配音暂停，进度仍跟随）");
    } else {
      show("🔊 配音模式");
      syncNow(true);
      ensurePlaying();
    }
  };
  document.documentElement.appendChild(tgl);

  function fail(msg) {
    if (timer) clearInterval(timer);
    show("❌ " + msg, 15000);
    btn.style.display = "block";
  }

  // ------------------------------------------------------------- 音轨挑选

  function sniff() {
    const isMedia = (u) => {
      const path = u.split("?")[0];
      return MEDIA_RE.test(path) || STREAM_RE.test(u);
    };
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
        if (/\.(m4a|aac|mp3|flac|ogg|wav)(\?|#|$)/i.test(c.url.split("?")[0])) s += 1;
        if (/\.m4s(\?|#|$)/i.test(c.url.split("?")[0])) s += 1;
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
    mode: "dub", // dub | orig
    elems: [new Audio(), new Audio()], blobs: {}, // k -> objectURL
  };
  const el = () => player.elems[player.curEl];
  const other = () => player.elems[1 - player.curEl];

  async function blobUrl(k) {
    if (player.blobs[k]) return player.blobs[k];
    const r = await xfetch(`${SERVER}/api/job/${jobId}/audio/${k}`, {}, 60000);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const u = URL.createObjectURL(await r.blob());
    player.blobs[k] = u;
    return u;
  }

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

    const chunks = (st.chunks || []).map((c) => ({ ...c }));
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
    if (st.status === "partial") {
      // 部分段失败：已完成的照常可听，点「重新配音」续跑失败段
      clearInterval(timer);
      const badN = chunks.filter((c) => c.status === "error").length;
      if (!player.started && chunks[0] && chunks[0].status === "done") startPlayer();
      show(`⚠️ ${doneN}/${chunks.length} 段完成，${badN} 段失败——点「重新配音」重试失败段`, 15000);
      btn.textContent = "🎙 重新配音";
      btn.style.display = "block";
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
    player.mode = "dub";
    video.muted = true;
    bind(video);
    show("🔊 AI 配音播放中（分段流式；未完成段自动回退原声）");
    btn.textContent = "🎙 重新配音";
    btn.style.display = "block";
    tgl.style.display = "block";
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
    if (player.mode === "orig") {
      el().pause();
      video.muted = false; // 原声模式：彻底让位给原视频
      return;
    }
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
      const preloaded = nEl.dataset.chunk === String(c.i) && nEl.readyState >= 2;
      if (needSwitch && preloaded) {
        // 乒乓切换：预载好的元素瞬时接管，无加载缺口
        el().pause();
        nEl.playbackRate = video.playbackRate * player.align;
        nEl.currentTime = off;
        if (!video.paused) nEl.play().catch((e) => show("❌ 换段播放失败：" + e.message, 6000));
        player.curEl = 1 - player.curEl;
        player.cur = c.i;
        video.muted = true;
        preloadNext(c.i);
        console.log(`[dub] 段${c.i} 乒乓接管 off=${off.toFixed(1)}s`);
      } else {
        // blob 路径：fetch → objectURL（媒体元素直连 localhost 会被 PNA 拦）
        blobUrl(c.i).then((u) => {
          if (player.cur !== c.i && !force) return;
          const a = el();
          if (a.dataset.chunk !== String(c.i)) {
            a.dataset.chunk = String(c.i);
            a.src = u;
            a.volume = 1;
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
          player.cur = c.i;
          video.muted = true;
          preloadNext(c.i);
          console.log(`[dub] 段${c.i} blob 播放 off=${off.toFixed(1)}s`);
        }).catch((e) => show("❌ 取段音频失败：" + e.message, 8000));
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

  function preloadNext(i) {
    const nxt = player.chunks.find((x) => x.i === i + 1);
    if (nxt && nxt.status === "done") {
      blobUrl(nxt.i).then((u) => {
        const o = other();
        if (o.dataset.chunk !== String(nxt.i)) {
          o.dataset.chunk = String(nxt.i);
          o.src = u;
        }
      }).catch(() => {});
    }
  }
})();
