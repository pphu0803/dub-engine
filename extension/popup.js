/** popup.js — 引擎状态灯 + 当前任务 + 默认语言/状态灯/引擎端口。纯扩展页面，无 background。 */
(() => {
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
      const r = await xfetch(`${DubServer.base()}/api/health`);
      h = await r.json();
      ping.textContent = `${Math.round(performance.now() - t0)}ms`;
    } catch (_) {
      ping.textContent = "";
    }
    if (!h || !h.ok) {
      dot.className = "dot down";
      line.textContent = DubI18n.t("p.offline", { port: DubServer.port });
      $("barWrap").style.display = "none";
      $("chips").innerHTML = "";
      return;
    }
    dot.className = `dot ${h.jobs && h.jobs.running > 0 ? "busy" : "up"}`;
    const chips = [
      [`${DubI18n.t("o.pipe")} ${h.pipeline_ver}`, false],
      [`TTS ${h.tts_backend}`, false],
      [h.llm_set ? `LLM ${h.llm_model || DubI18n.t("p.chipSet")}` : DubI18n.t("p.chipLlmMissing"), !h.llm_set],
      [h.mimo_set ? "MiMo ✓" : "MiMo ✗", !h.mimo_set],
    ];
    $("chips").innerHTML = chips
      .map(([c, warn]) => `<span class="chip${warn ? " warn" : ""}">${c}</span>`)
      .join("");

    // 当前任务：内存里最近的 running/partial 优先，否则显示最近一条
    try {
      const r = await xfetch(`${DubServer.base()}/api/jobs`);
      const jobs = (await r.json()).jobs || [];
      const act = jobs.find((j) => j.status === "running") ||
                  jobs.find((j) => j.status === "partial") || jobs[0];
      if (!act) {
        line.textContent = DubI18n.t("p.noJobs");
        $("barWrap").style.display = "none";
        return;
      }
      const pct = act.chunks_total ? Math.round(100 * act.chunks_done / act.chunks_total) : 0;
      const STAT_KEY = { running: "st.running", done: "st.done", partial: "st.partial", error: "st.error" }[act.status];
      const stat = STAT_KEY ? DubI18n.t(STAT_KEY) : act.status;
      line.textContent = DubI18n.t("p.jobLine", {
        status: stat, done: act.chunks_done, total: act.chunks_total,
        lang: act.lang || "?", tts: act.tts_backend || "?",
      });
      $("barWrap").style.display = "block";
      $("fill").style.width = pct + "%";
    } catch (_) {
      line.textContent = DubI18n.t("p.jobsFail");
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

  // 引擎端口：与 python -m dub serve --port 对应；改动即生效（状态灯自动按新端口重连）
  const portIn = $("port");
  try {
    chrome.storage.local.get({ dub_port: 8765 }, (r) => { portIn.value = r.dub_port; });
  } catch (_) { portIn.value = DubServer.port; }
  portIn.onchange = () => {
    const p = parseInt(portIn.value, 10);
    if (!(p >= 1 && p <= 65535)) { portIn.value = DubServer.port; return; } // 非法输入回退现值
    portIn.value = p;
    try { chrome.storage.local.set({ dub_port: p }); } catch (_) {}
  };

  // 界面语言：chrome.storage.local.dub_ui_lang（""=自动跟随浏览器），i18n.js 即时切换
  const uiLangSel = $("uiLang");
  try {
    chrome.storage.local.get({ dub_ui_lang: "" }, (r) => { uiLangSel.value = r.dub_ui_lang || ""; });
  } catch (_) {}
  uiLangSel.onchange = () => {
    try { chrome.storage.local.set({ dub_ui_lang: uiLangSel.value }); } catch (_) {}
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
    if (tabId == null) { $("jobLine").textContent = DubI18n.t("p.unsupported"); return; }
    try {
      chrome.tabs.sendMessage(tabId, { cmd: "dub" }, () => {
        if (chrome.runtime.lastError) {
          $("jobLine").textContent = DubI18n.t("p.unsupportedRefresh");
          return;
        }
        window.close();
      });
    } catch (_) {
      $("jobLine").textContent = DubI18n.t("p.unsupportedShort");
    }
  };

  $("open").onclick = () => {
    try { chrome.runtime.openOptionsPage(); }
    catch (_) { window.open(chrome.runtime.getURL("options.html")); }
  };
  $("refresh").onclick = refresh;
  DubServer.init(refresh); // 首次读到存储端口、以及端口变更时，都重刷一次状态
  DubI18n.init(refresh);   // 语言确定/切换后重刷动态文本（静态文本由 applyDOM 负责）
  setInterval(refresh, 8000);
})();
