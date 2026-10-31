/**
 * content.js — 视频页注入按钮/状态 pill/同步播放器，并驱动整个流程
 *
 * 架构：content 是司机（页面常驻，轮询引擎状态），SW 只做无状态代理。
 * 音轨候选来自 performance 资源历史（页面记全账）+ video src。
 * 就绪后：原声静音，Audio 元素播配音母版，按 video.currentTime 同步。
 */
(() => {
  if (window.__dubInstalled) return;
  window.__dubInstalled = true;

  // 页面资源历史默认只留 ~250 条，长页面会挤掉音轨请求，扩容
  try { performance.setResourceTimingBufferSize(4000); } catch (_) {}

  const MEDIA_RE = /\.(m4s|mp4|m4a|aac|webm|mp3|ogg|wav|flac)(\?|#|$)/i;
  const STAGE_ZH = {
    extract: "抽音轨", separate: "Demucs 分离", asr: "Whisper 识别",
    translate: "LLM 翻译", tts: "配音合成", assemble: "混音", encode: "转码",
  };

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

  function sniff() {
    const urls = new Set();
    for (const e of performance.getEntriesByType("resource")) {
      if (/^https?:/.test(e.name) && MEDIA_RE.test(e.name)) urls.add(e.name);
    }
    document.querySelectorAll("video,audio,source").forEach((el) => {
      if (el.src) urls.add(el.src);
      if (el.currentSrc) urls.add(el.currentSrc);
    });
    try {
      const pr = window.ytInitialPlayerResponse;
      (pr?.streamingData?.adaptiveFormats || [])
        .filter((f) => (f.mimeType || "").startsWith("audio/") && f.url)
        .forEach((f) => urls.add(f.url));
    } catch (_) { /* 非 YouTube */ }
    return [...urls];
  }

  let timer = null;
  let jobId = null;

  async function begin() {
    btn.style.display = "none";
    show("读页面记录…");
    let res;
    try {
      res = await chrome.runtime.sendMessage({
        type: "dub-begin", urls: sniff(), pageUrl: location.href, lang: "zh",
      });
    } catch (e) {
      fail("后台未响应（扩展刚刷新过？）：" + e.message);
      return;
    }
    if (!res || res.error) {
      fail((res && res.error) || "未知错误");
      return;
    }
    jobId = res.jobId;
    show("⏳ 排队…");
    timer = setInterval(poll, 3000);
    poll();
  }

  async function poll() {
    let st;
    try {
      st = await chrome.runtime.sendMessage({ type: "job-status", jobId });
    } catch (_) {
      return; // SW 短暂不可用，下轮再试
    }
    if (!st) return;
    if (st.error) { fail(st.error); return; }
    if (st.status === "error") { fail(st.error || "引擎出错"); return; }
    if (st.status === "done") {
      clearInterval(timer);
      show("⏳ 取回音频…");
      try {
        const au = await chrome.runtime.sendMessage({ type: "job-audio", jobId });
        if (!au || au.error) throw new Error((au && au.error) || "空响应");
        const bin = atob(au.b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        attach(URL.createObjectURL(new Blob([arr], { type: "audio/mpeg" })));
      } catch (e) {
        fail("取音频失败：" + e.message);
      }
      return;
    }
    show(`⏳ ${STAGE_ZH[st.stage] || st.stage || "处理中"}`);
  }

  function fail(msg) {
    if (timer) clearInterval(timer);
    show("❌ " + msg, 12000);
    btn.style.display = "block";
  }

  // ------------------------------------------------------------------ //

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
