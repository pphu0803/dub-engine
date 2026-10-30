/**
 * content.js — 视频页注入「AI 配音」按钮 + 状态 pill + 同步播放器
 *
 * 就绪后：原声静音，Audio 元素播配音母版（含伴奏），按 video.currentTime
 * 同步（play/pause/seek/rate 全跟随，漂移 >0.35s 自动校正）。
 */
(() => {
  if (window.__dubInstalled) return;
  window.__dubInstalled = true;

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
  btn.onclick = () => {
    btn.style.display = "none";
    show("准备中…");
    chrome.runtime.sendMessage({ type: "dub-start", lang: "zh" });
  };
  document.documentElement.appendChild(btn);

  let dub = null; // {audio, video}

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

  function attach(url) {
    const vids = [...document.querySelectorAll("video")].filter((v) => v.duration > 0 || v.src || v.currentSrc);
    const video = vids.sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0];
    if (!video) { show("页面上没找到 <video>", 4000); return; }
    video.muted = true;
    const audio = new Audio(url);
    audio.preload = "auto";
    dub = { audio, video };
    sync(video, audio);
    if (!video.paused) { audio.currentTime = video.currentTime; audio.play().catch(() => {}); }
    show("🔊 已切换 AI 配音（原声已静音）", 3500);
    btn.textContent = "🎙 已配音";
    btn.style.display = "block";
  }

  chrome.runtime.onMessage.addListener((m) => {
    if (m.type === "dub-status") {
      if (m.stage === "error") show("❌ " + (m.detail || "失败"), 8000);
      else show(`⏳ ${m.stage}${m.detail ? " · " + m.detail : ""}`);
    } else if (m.type === "dub-ready") {
      attach(m.url);
    }
  });
})();
