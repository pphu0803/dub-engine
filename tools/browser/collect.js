/**
 * collect.js — 在视频页面里找出"音轨地址"
 *
 * 用法：
 *   1. 打开目标视频页，按 F12 → Console
 *   2. 整段粘贴本文件，回车
 *   3. 等 2–5 秒，会打印一张候选地址表；把 best 那一行的 URL 复制出来
 *   4. 回到命令行：python tools/probe_media.py --url "<那个URL>" --referer "<当前页面URL>"
 *
 * 说明：
 *   - 只读取浏览器"已经发出的请求"，不额外下载视频
 *   - 每个候选只取前 1 MB 来测速，不会把整片拉下来
 *   - 站点自己有防盗链时，把 --referer 和（必要时）--cookie 带上
 */
(async () => {
  const MB = 1024 * 1024;
  const PROBE_BYTES = 1 * MB;

  const MEDIA_RE = /\.(m3u8|mpd|m4s|mp4|m4a|aac|ts|webm)(\?|#|$)/i;
  const AUDIO_HINT = /(audio|aud|aac|m4a|140|251|dash_audio)/i;

  const uniq = (arr) => [...new Set(arr.filter(Boolean))];

  // 1) 从 performance 里捞
  const entries = performance.getEntriesByType("resource")
    .map((e) => ({ url: e.name, type: e.initiatorType, size: e.transferSize || 0 }))
    .filter((e) => e.url.startsWith("http") && MEDIA_RE.test(e.url));

  // 2) 从 DOM 里捞（有些站点的 src 不在 performance 里）
  const domUrls = [];
  document.querySelectorAll("video, audio, source").forEach((el) => {
    if (el.src) domUrls.push(el.src);
    if (el.currentSrc) domUrls.push(el.currentSrc);
    el.querySelectorAll?.("source").forEach((s) => s.src && domUrls.push(s.src));
  });

  // 3) YouTube 特例：从 ytInitialPlayerResponse 里取音频格式
  try {
    const pr = window.ytInitialPlayerResponse;
    const fmts = pr?.streamingData?.adaptiveFormats || [];
    fmts.filter((f) => (f.mimeType || "").startsWith("audio/"))
        .forEach((f) => domUrls.push(f.url));
  } catch (_) { /* 非 YouTube 页面，忽略 */ }

  const candidates = uniq([...entries.map((e) => e.url), ...domUrls])
    .filter((u) => MEDIA_RE.test(u) || AUDIO_HINT.test(u));

  if (!candidates.length) {
    console.warn("[dub] 没找到候选。先让视频播 2 秒，再运行一次；或在 Network 面板里筛选 media。");
    return;
  }

  const measure = async (url) => {
    const t0 = performance.now();
    try {
      const res = await fetch(url, {
        method: "GET",
        headers: { Range: `bytes=0-${PROBE_BYTES - 1}` },
        credentials: "include",
        cache: "no-store",
      });
      const buf = await res.arrayBuffer();
      const ms = performance.now() - t0;
      const cr = res.headers.get("content-range") || "";
      const total = cr.includes("/") ? Number(cr.split("/")[1]) : 0;
      return {
        ok: res.ok,
        status: res.status,
        type: res.headers.get("content-type") || "",
        got: buf.byteLength,
        total,
        ms,
        mbps: ms > 0 ? buf.byteLength / MB / (ms / 1000) : 0,
      };
    } catch (e) {
      return { ok: false, status: 0, err: String(e), got: 0, total: 0, ms: 0, mbps: 0 };
    }
  };

  console.log(`[dub] 找到 ${candidates.length} 个候选，开始逐个测速…`);
  const rows = [];
  for (const url of candidates.slice(0, 12)) {
    const m = await measure(url);
    rows.push({
      url,
      isPlaylist: /\.(m3u8|mpd)(\?|#|$)/i.test(url),
      kind: /\.(m3u8|mpd)(\?|#|$)/i.test(url)
        ? "playlist"
        : (AUDIO_HINT.test(url) ? "audio?" : "media"),
      ...m,
    });
  }

  rows.sort((a, b) => (b.mbps || 0) - (a.mbps || 0));

  console.table(rows.map((r) => ({
    kind: r.kind,
    status: r.status,
    "MB/s": r.mbps ? r.mbps.toFixed(1) : "-",
    取到: `${(r.got / MB).toFixed(2)} MB`,
    总大小: r.total ? `${(r.total / MB).toFixed(1)} MB` : "?",
    type: (r.type || "").slice(0, 40),
    url: r.url.slice(0, 110) + (r.url.length > 110 ? "…" : ""),
  })));

  const best = rows.find((r) => r.ok && !r.isPlaylist && r.mbps > 0) || rows[0];
  console.log("%c[dub] 建议拿这条去跑 probe_media.py：", "color:#0a0;font-weight:bold");
  console.log(best.url);
  console.log("[dub] 页面地址（作 --referer 用）：", location.href);

  window.__dubRows = rows; // 需要原始数据时在控制台看这个
  return rows;
})();
