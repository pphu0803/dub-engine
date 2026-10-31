/** popup.js — 引擎状态灯 + 当前任务 + 默认语言/状态灯开关。纯扩展页面，无 background。 */
(() => {
  const SERVER = "http://127.0.0.1:8765";
  const $ = (id) => document.getElementById(id);

  async function xfetch(url, timeoutMs = 4000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await fetch(url, { signal: ctrl.signal });
    } finally {
      clearTimeout(t);
    }
  }

  async function refresh() {
    const dot = $("dot"), ping = $("ping"), line = $("jobLine");
    let h = null;
    try {
      const t0 = performance.now();
      const r = await xfetch(`${SERVER}/api/health`);
      h = await r.json();
      ping.textContent = `${Math.round(performance.now() - t0)}ms`;
    } catch (_) {
      ping.textContent = "";
    }
    if (!h || !h.ok) {
      dot.className = "dot down";
      line.textContent = "引擎离线——启动 python -m dub serve";
      $("barWrap").style.display = "none";
      $("chips").innerHTML = "";
      return;
    }
    dot.className = `dot ${h.jobs && h.jobs.running > 0 ? "busy" : "up"}`;
    const chips = [
      `流水线 ${h.pipeline_ver}`, `TTS ${h.tts_backend}`,
      h.llm_set ? `LLM ${h.llm_model || "已配置"}` : "LLM 未配置",
      h.mimo_set ? "MiMo ✓" : "MiMo ✗",
    ];
    $("chips").innerHTML = chips
      .map((c) => `<span class="chip${c.includes("未配置") || c.includes("✗") ? " warn" : ""}">${c}</span>`)
      .join("");

    // 当前任务：内存里最近的 running/partial 优先，否则显示最近一条
    try {
      const r = await xfetch(`${SERVER}/api/jobs`);
      const jobs = (await r.json()).jobs || [];
      const act = jobs.find((j) => j.status === "running") ||
                  jobs.find((j) => j.status === "partial") || jobs[0];
      if (!act) {
        line.textContent = "暂无任务";
        $("barWrap").style.display = "none";
        return;
      }
      const pct = act.chunks_total ? Math.round(100 * act.chunks_done / act.chunks_total) : 0;
      const stat = { running: "⏳ 配音中", done: "✅ 完成", partial: "⚠️ 部分完成", error: "❌ 失败" }[act.status] || act.status;
      line.textContent = `${stat} ${act.chunks_done}/${act.chunks_total} 段 · ${act.lang || "?"} · ${act.tts_backend || "?"}`;
      $("barWrap").style.display = "block";
      $("fill").style.width = pct + "%";
    } catch (_) {
      line.textContent = "任务状态获取失败";
    }
  }

  // 默认语言与状态灯开关：chrome.storage 为准（content.js 优先读它），localStorage 兜底
  const langSel = $("lang"), hud = $("hud");
  try {
    chrome.storage.local.get({ dub_lang: null, dub_hud: true }, (r) => {
      if (r.dub_lang) langSel.value = r.dub_lang;
      hud.checked = r.dub_hud !== false;
    });
  } catch (_) { /* 扩展上下文不可用（不该发生） */ }
  langSel.onchange = () => {
    try { chrome.storage.local.set({ dub_lang: langSel.value }); } catch (_) {}
  };
  hud.onchange = () => {
    try { chrome.storage.local.set({ dub_hud: hud.checked }); } catch (_) {}
  };

  $("open").onclick = () => {
    try { chrome.runtime.openOptionsPage(); }
    catch (_) { window.open(chrome.runtime.getURL("options.html")); }
  };
  $("refresh").onclick = refresh;
  refresh();
  setInterval(refresh, 8000);
})();
