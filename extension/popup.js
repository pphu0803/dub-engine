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

  // 默认语言与两个开关：chrome.storage 为准（content.js 优先读它），localStorage 兜底
  const langSel = $("lang"), hud = $("hud"), siteBtn = $("siteBtn");
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

  // 此站显示配音按钮：按站点记忆（origin 粒度）。activeTab 权限在点开面板时
  // 临时可读当前标签 url；非网页（chrome:// 等）隐藏该开关
  let tabOrigin = null, tabId = null;
  try {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const t = tabs && tabs[0];
      if (!t || !t.url) { siteBtn.closest(".switch").style.display = "none"; return; }
      let u;
      try { u = new URL(t.url); } catch (_) { siteBtn.closest(".switch").style.display = "none"; return; }
      if (u.protocol !== "http:" && u.protocol !== "https:") {
        siteBtn.closest(".switch").style.display = "none";
        return;
      }
      tabId = t.id;
      tabOrigin = u.origin;
      chrome.storage.local.get({ dub_btn_sites: [] }, (r) => {
        siteBtn.checked = (r.dub_btn_sites || []).includes(tabOrigin);
      });
    });
  } catch (_) {}
  siteBtn.onchange = () => {
    if (!tabOrigin) return;
    chrome.storage.local.get({ dub_btn_sites: [] }, (r) => {
      const arr = new Set(r.dub_btn_sites || []);
      if (siteBtn.checked) arr.add(tabOrigin);
      else arr.delete(tabOrigin);
      chrome.storage.local.set({ dub_btn_sites: Array.from(arr) });
    });
  };

  // 配音此页：发消息给 content script 开配（按钮藏着他也能配）
  $("dubNow").onclick = () => {
    if (tabId == null) { $("jobLine").textContent = "此页不支持（非普通网页）"; return; }
    try {
      chrome.tabs.sendMessage(tabId, { cmd: "dub" }, () => {
        if (chrome.runtime.lastError) {
          $("jobLine").textContent = "此页不支持（插件未注入，刷新页面试试）";
          return;
        }
        window.close();
      });
    } catch (_) {
      $("jobLine").textContent = "此页不支持";
    }
  };

  $("open").onclick = () => {
    try { chrome.runtime.openOptionsPage(); }
    catch (_) { window.open(chrome.runtime.getURL("options.html")); }
  };
  $("refresh").onclick = refresh;
  refresh();
  setInterval(refresh, 8000);
})();
