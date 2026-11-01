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

  // 引擎基址：端口由 dub-server.js 从 chrome.storage 读（面板可改，默认 8765）
  DubServer.init();
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

  function show(html, ms, always) {
    // 页面状态灯（插件面板/管理页可关）：关闭后进度提示只进 console，错误仍会弹出
    if (!hudOn && !always) {
      console.log("[dub]", html.replace(/<[^>]*>/g, " "));
      return;
    }
    pill.innerHTML = html;
    pill.style.display = "block";
    if (ms) setTimeout(() => (pill.style.display = "none"), ms);
  }

  // 插件偏好：chrome.storage 为准（面板/管理页可改，跨站点统一），localStorage 兜底
  let hudOn = true;
  let lang = localStorage.getItem("dub_lang") || "zh";
  try {
    chrome.storage.local.get({ dub_lang: null, dub_hud: true, dub_btn_sites: [] }, (r) => {
      if (r.dub_lang) { lang = r.dub_lang; langSel.value = lang; }
      hudOn = r.dub_hud !== false;
      btnSites = new Set(r.dub_btn_sites || []);
      updateBtnVis();
    });
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area !== "local") return;
      if ("dub_hud" in ch) hudOn = ch.dub_hud.newValue !== false;
      if ("dub_lang" in ch) { lang = ch.dub_lang.newValue; langSel.value = lang; }
      if ("dub_btn_sites" in ch) { btnSites = new Set(ch.dub_btn_sites.newValue || []); updateBtnVis(); }
    });
  } catch (_) { /* 无扩展上下文时退 localStorage */ }

  const btn = document.createElement("button");
  btn.textContent = "🎙 AI 配音";
  Object.assign(btn.style, {
    position: "fixed", top: "58px", right: "12px", zIndex: 2147483647, // pill 下方，不遮挡进度
    padding: "8px 16px", borderRadius: "999px", border: "0", cursor: "pointer",
    font: "600 13px/1.4 system-ui", color: "#04140c",
    background: "linear-gradient(135deg,#42e695,#3bb2b8)",
    boxShadow: "0 2px 10px rgba(0,0,0,.35)", display: "none",
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

  // 配音输出语言：chrome.storage 优先（面板/管理页可改），写回双写保持兼容
  const langSel = document.createElement("select");
  langSel.innerHTML =
    '<option value="zh">🌐 中文</option>' +
    '<option value="en">🌐 English</option>' +
    '<option value="ja">🌐 日本語</option>' +
    '<option value="ko">🌐 한국어</option>';
  langSel.value = lang;
  Object.assign(langSel.style, {
    position: "fixed", top: "58px", right: "136px", zIndex: 2147483647,
    padding: "7px 12px", borderRadius: "999px", border: "0", cursor: "pointer",
    font: "600 13px/1.4 system-ui", color: "#fff",
    background: "rgba(20,20,24,.82)", boxShadow: "0 2px 10px rgba(0,0,0,.35)",
    appearance: "none", WebkitAppearance: "none", display: "none",
  });
  langSel.title = "配音输出语言（对当前任务无影响，下次点击生效）";
  langSel.onchange = () => {
    lang = langSel.value;
    localStorage.setItem("dub_lang", lang);
    try { chrome.storage.local.set({ dub_lang: lang }); } catch (_) {}
  };
  document.documentElement.appendChild(langSel);

  function fail(msg) {
    if (timer) clearInterval(timer);
    show("❌ " + msg, 15000, true);
    updateBtnVis();
  }

  // 页面按钮可见性：默认不在网页常驻（非视频页常驻按钮是用户痛点）。
  // 本站在面板开启「此站显示配音按钮」、或本页配音会话进行中（需要能切回原声）才显示
  let btnSites = new Set();
  function updateBtnVis() {
    const show = btnSites.has(location.origin) || player.started;
    btn.style.display = show ? "block" : "none";
    langSel.style.display = show ? "block" : "none";
  }

  // 面板「配音此页」入口：无 background，content script 直接收消息开配
  try {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.cmd === "dub" && !timer) {
        begin().catch((e) => fail("启动失败：" + e.message));
      }
    });
  } catch (_) {}

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
          // 媒体元素正在播的直链是强信号：B 站番剧试看等渐进式 mp4 页面
          // 没有 DASH 音轨可挑，只有这一条路
          out.push({ url: u, size: 0, forced: false, fromVideo: el.tagName === "VIDEO" });
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
        if (c.fromVideo) s += 3;
        if (AUDIO_HINT.test(c.url)) s += 2;
        if (/itag=(140|139|141|599|600)\b/i.test(c.url)) s += 2;
        if (/\.(m4a|aac|mp3|flac|ogg|wav)(\?|#|$)/i.test(c.url.split("?")[0])) s += 1;
        if (/\.m4s(\?|#|$)/i.test(c.url.split("?")[0])) s += 1;
        if (c.size > 300 * 1024) s += 2;
        return { ...c, s };
      })
      .filter((c) => c.s >= 3 && (c.forced || c.fromVideo || AUDIO_HINT.test(c.url)))
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
    let body;
    if (c) {
      console.log("[dub] 音轨:", c.url, "score", c.s);
      body = { url: c.url, referer: location.href, lang };
    } else {
      // 抓不到直链（播放器变种/防 sniff）：退 page_url 模式，引擎用 yt-dlp 直取
      show("⏳ 页内没抓到音轨，改用引擎 yt-dlp 直取…");
      body = { page_url: location.href, referer: location.href, lang };
    }
    let job;
    try {
      const r = await xfetch(DubServer.base() + "/api/dub", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }, 60000); // page_url 模式服务端要先落音频文件再返回
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
    dubDuration: 0, // 当前配音对应的视频时长（媒体身份锚点，换集检测用）
  };

  // 连播换集：B 站自动播下一集时同一页面换源，配音还是上一集的 → 立即
  // 停配音、放开原声、重置状态。时长差 >3s 判为不同媒体（URL 不可靠：
  // YT 拖进度条也改 URL）。同集内换清晰度时长不变，不会误触发。
  function resetForNewMedia() {
    if (timer) { clearInterval(timer); timer = null; }
    try { el().pause(); other().pause(); } catch (_) {}
    for (const u of Object.values(player.blobs)) URL.revokeObjectURL(u);
    const v = biggestVideo();
    if (v) v.muted = false;
    player.started = false;
    player.video = null;
    player.chunks = [];
    player.cur = -1;
    player.curEl = 0;
    player.blobs = {};
    player.align = 1;
    player.dubDuration = 0;
    jobId = null;
    tgl.style.display = "none";
    tgl.textContent = "🔈 切回原声";
    btn.textContent = "🎙 AI 配音";
    updateBtnVis();
    show("⚠️ 检测到视频已切换（自动连播下一集）——配音是上一集的，已切回原声。点「AI 配音」为新视频配音", 12000, true);
  }

  function mediaChanged(v) {
    return player.started && player.dubDuration > 0 &&
      v && isFinite(v.duration) && v.duration > 0 &&
      Math.abs(v.duration - player.dubDuration) > 3;
  }
  const el = () => player.elems[player.curEl];
  const other = () => player.elems[1 - player.curEl];

  async function blobUrl(k) {
    if (player.blobs[k]) return player.blobs[k];
    const r = await xfetch(`${DubServer.base()}/api/job/${jobId}/audio/${k}`, {}, 60000);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const u = URL.createObjectURL(await r.blob());
    player.blobs[k] = u;
    return u;
  }

  async function poll() {
    let st;
    try {
      // 轮询捎带当前播放位置：引擎优先配"离你最近的段"（拖到哪配到哪）
      const v = player.video || biggestVideo();
      const t = v && isFinite(v.currentTime) ? v.currentTime : -1;
      const q = t >= 0 ? `?t=${t.toFixed(1)}` : "";
      const r = await xfetch(`${DubServer.base()}/api/job/${jobId}${q}`);
      st = await r.json();
    } catch (_) {
      return; // 引擎忙/瞬时失联，下轮再试
    }
    if (st.duration) player.dubDuration = st.duration;
    // 兜底：loadedmetadata 若错过（绑定晚于换源），轮询时再核一次时长
    if (mediaChanged(player.video || biggestVideo())) { resetForNewMedia(); return; }
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
        updateBtnVis();
      } else if (chunks.length) {
        startPlayer();
      }
      return;
    }
    if (st.status === "partial") {
      // 部分段失败：已完成的照常可听，点「重新配音」续跑失败段
      clearInterval(timer);
      const badN = chunks.filter((c) => c.status === "error").length;
      if (!player.started && chunks.some((c) => c.status === "done")) startPlayer();
      show(`⚠️ ${doneN}/${chunks.length} 段完成，${badN} 段失败——点「重新配音」重试失败段`, 15000);
      btn.textContent = "🎙 重新配音";
      updateBtnVis();
      return;
    }
    if (st.status === "running") {
      if (!chunks.length) {
        const STAGE_CN = { download: "yt-dlp 取音频", extract: "抽取整轨" };
        show(`⏳ ${STAGE_CN[st.stage] || st.stage || "处理中"}…`);
      } else {
        show(`⏳ 配音中 ${doneN}/${chunks.length} 段`);
        if (player.started) syncNow(); // 新段就绪后，若正停在未完成段上则切回配音
      }
      if (!player.started && readyAt(chunks)) {
        startPlayer();
      }
    }
  }

  function biggestVideo() {
    return [...document.querySelectorAll("video")]
      .filter((v) => v.duration > 0 || v.src || v.currentSrc)
      .sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0] || null;
  }

  // 起播门控：播放头所在段就绪即可开播（拖到哪，先出哪的声）。
  // 找不到 video 或位置不在任何段内，退回旧的"段 0 完成"判定
  function readyAt(chunks) {
    const v = player.video || biggestVideo();
    if (!v) return !!(chunks[0] && chunks[0].status === "done");
    const t = v.currentTime;
    const c = chunks.find((x) => t >= x.start - 0.05 && t < x.end + 0.05);
    return c ? c.status === "done" : !!(chunks[0] && chunks[0].status === "done");
  }

  function startPlayer() {
    const video = biggestVideo();
    if (!video) { show("页面上没找到 <video>", 6000, true); updateBtnVis(); return; }
    player.started = true;
    player.video = video;
    player.mode = "dub";
    video.muted = true;
    bind(video);
    show("🔊 AI 配音播放中（分段流式；未完成段自动回退原声）");
    btn.textContent = "🎙 重新配音";
    updateBtnVis();
    tgl.style.display = "block";
    syncNow(true);
    ensurePlaying();
  }

  function bind(video) {
    if (video.__dubBound) return;
    video.__dubBound = true;
    video.addEventListener("play", () => { syncNow(true); ensurePlaying(); });
    video.addEventListener("pause", () => el().pause());
    video.addEventListener("seeked", () => { syncNow(true); ensurePlaying(); });
    video.addEventListener("ratechange", () => { el().playbackRate = video.playbackRate * player.align; });
    video.addEventListener("timeupdate", () => syncNow(false));
    // 换源完成（连播下一集/切视频）：元数据一到就核对时长，对不上立刻止损
    video.addEventListener("loadedmetadata", () => {
      if (mediaChanged(video)) resetForNewMedia();
    });
  }

  function ensurePlaying() {
    const a = el();
    const go = () => {
      if (player.video && !player.video.paused && a.paused) {
        a.play().catch((e) => show("❌ 配音播放受阻：" + e.message, 6000, true));
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
        if (!video.paused) nEl.play().catch((e) => show("❌ 换段播放失败：" + e.message, 6000, true));
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
        }).catch((e) => show("❌ 取段音频失败：" + e.message, 8000, true));
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
