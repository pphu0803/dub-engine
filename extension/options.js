/** options.js — 管理页逻辑：引擎状态 / LLM·MiMo 配置（写回 config.json 热生效）/ 插件偏好 / 任务管理。
 * 配置语义与引擎一致：保存时 key 留空 = 保持现值；「清除」= 发 null。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const STAT = {
    running: ["st.running", "run"], done: ["st.done", "done"],
    partial: ["st.partial", "warn"], error: ["st.error", "err"],
  };

  async function api(path, opts = {}, timeoutMs = 8000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(DubServer.base() + path, { signal: ctrl.signal, ...opts });
      return { code: r.status, data: await r.json().catch(() => ({})) };
    } finally {
      clearTimeout(t);
    }
  }

  const fmtDur = (s) => {
    if (!s || !isFinite(s)) return "—";
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m ? DubI18n.t("o.durMs", { m, r }) : DubI18n.t("o.durS", { r });
  };
  const fmtTime = (ts) => {
    if (!ts) return "—";
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                   : d.toLocaleDateString([], { month: "numeric", day: "numeric" }) + " " +
                     d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };

  // ------------------------------------------------------------- 引擎状态

  async function loadHealth() {
    const dot = $("dot");
    let h = null;
    try {
      h = (await api("/api/health", {}, 4000)).data;
    } catch (_) { /* 离线 */ }
    if (!h || !h.ok) {
      dot.className = "dot down";
      $("offline").style.display = "block";
      $("hVer").textContent = $("hPipe").textContent = $("hTts").textContent =
        $("hUp").textContent = $("hLlm").textContent = $("hMimo").textContent = "—";
      return false;
    }
    $("offline").style.display = "none";
    dot.className = "dot up";
    $("hVer").textContent = "serve " + h.version;
    $("hPipe").textContent = h.pipeline_ver;
    $("hTts").textContent = h.tts_backend;
    const up = h.uptime_s || 0;
    $("hUp").textContent = up > 3600 ? DubI18n.t("o.upHM", { h: Math.floor(up / 3600), m: Math.floor(up % 3600 / 60) })
                                     : up > 60 ? DubI18n.t("o.upMS", { m: Math.floor(up / 60), s: up % 60 })
                                               : DubI18n.t("o.upS", { s: up });
    $("hLlm").textContent = h.llm_set ? (h.llm_model || DubI18n.t("o.set")) : DubI18n.t("o.unset");
    $("hLlm").style.color = h.llm_set ? "" : "#e8c268";
    $("hMimo").textContent = h.mimo_set ? DubI18n.t("o.set") : DubI18n.t("o.unset");
    $("hMimo").style.color = h.mimo_set ? "" : "#9a9aa6";
    return true;
  }

  // ----------------------------------------------------------------- 配置

  let CFG = null; // 最近一次 GET /api/config 的 tts 段缓存
  const BUILTIN_PROVS = ["edge", "stub", "cosyvoice", "mimo", "openai"];
  const provName = () => {
    const v = $("provSel").value;
    return v === "__new" ? $("provNewName").value.trim() : v;
  };

  function fillConfig(cfg) {
    $("llmBase").value = cfg.llm?.base || "";
    $("llmModel").value = cfg.llm?.model || "";
    $("llmKey").placeholder = cfg.llm?.key_set ? DubI18n.t("o.keySet", { tail: cfg.llm.key_tail }) : DubI18n.t("o.keyUnset");
    CFG = cfg.tts || { backend: "edge", providers: {}, voice_maps: {} };
    const names = [...new Set([...BUILTIN_PROVS, ...Object.keys(CFG.providers || {})])];
    $("ttsBackend").innerHTML = names.map((n) => `<option value="${n}">${n}</option>`).join("");
    $("ttsBackend").value = CFG.backend || "edge";
    $("provSel").innerHTML = '<option value="">— 选择 —</option>'
      + names.map((n) => `<option value="${n}">${n}</option>`).join("")
      + '<option value="__new">＋ 新建…</option>';
    $("vmJson").value = JSON.stringify(CFG.voice_maps || {}, null, 2);
    loadProvFields();
  }

  function loadProvFields() {
    const name = $("provSel").value;
    const isNew = name === "__new";
    $("provNewName").style.display = isNew ? "block" : "none";
    const p = (CFG && CFG.providers || {})[name] || {};
    $("provType").value = p.type || (BUILTIN_PROVS.includes(name) ? name : "openai");
    $("provBase").value = p.base || "";
    $("provModel").value = p.model || "";
    $("provKey").value = "";
    $("provKey").placeholder = p.key_set ? DubI18n.t("o.keySet", { tail: p.key_tail }) : DubI18n.t("o.keyUnset");
    $("provRoot").value = p.root || "";
    $("provRef").value = p.ref_wav || "";
  }

  async function loadConfig() {
    try {
      fillConfig((await api("/api/config")).data);
    } catch (_) { /* 离线时保持占位 */ }
  }

  async function saveConfig(body, msgEl) {
    msgEl.className = "msg";
    msgEl.textContent = DubI18n.t("o.saving");
    try {
      const { code, data } = await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (code !== 200) throw new Error(data.error || code);
      fillConfig(data);
      msgEl.className = "msg ok";
      msgEl.textContent = DubI18n.t("o.saved");
      loadHealth();
    } catch (e) {
      msgEl.className = "msg err";
      msgEl.textContent = "❌ " + e.message;
    }
  }

  async function testTarget(target, body, msgEl) {
    msgEl.className = "msg";
    msgEl.textContent = DubI18n.t("o.testing");
    try {
      const { data } = await api("/api/config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, ...body }),
      }, 50000);
      if (!data.ok) throw new Error(data.error || "failed");
      msgEl.className = "msg ok";
      msgEl.textContent = DubI18n.t("o.connected", { ms: data.latency_ms });
    } catch (e) {
      msgEl.className = "msg err";
      msgEl.textContent = "❌ " + e.message;
    }
  }

  $("llmSave").onclick = () => saveConfig({
    llm: { base: $("llmBase").value.trim(), model: $("llmModel").value.trim(), key: $("llmKey").value },
  }, $("llmMsg"));
  $("llmTest").onclick = () => testTarget("llm", {
    llm: { base: $("llmBase").value.trim(), model: $("llmModel").value.trim(), key: $("llmKey").value },
  }, $("llmMsg"));

  $("provSel").onchange = loadProvFields;
  $("ttsSave").onclick = () => {
    const name = provName();
    if (!name) { $("ttsMsg").className = "msg err"; $("ttsMsg").textContent = DubI18n.t("o.needProv"); return; }
    const pc = { type: $("provType").value };
    if ($("provBase").value.trim()) pc.base = $("provBase").value.trim();
    if ($("provModel").value.trim()) pc.model = $("provModel").value.trim();
    if ($("provKey").value) pc.key = $("provKey").value;
    if ($("provRoot").value.trim()) pc.root = $("provRoot").value.trim();
    if ($("provRef").value.trim()) pc.ref_wav = $("provRef").value.trim();
    saveConfig({ tts: { providers: { [name]: pc } } }, $("ttsMsg"));
  };
  $("ttsSetDefault").onclick = () => {
    const name = provName() || $("ttsBackend").value;
    saveConfig({ tts_backend: name }, $("ttsMsg"));
  };
  $("ttsTest").onclick = async () => {
    const name = provName() || $("ttsBackend").value;
    const msgEl = $("ttsMsg");
    msgEl.className = "msg";
    msgEl.textContent = DubI18n.t("o.synth");
    try {
      const { data } = await api("/api/config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: "tts", provider: name, voice: $("provTestVoice").value.trim() }),
      }, 65000);
      if (!data.ok) throw new Error(data.error || "failed");
      msgEl.className = "msg ok";
      msgEl.textContent = DubI18n.t("o.connected", { ms: data.latency_ms });
      const au = $("ttsAudio");
      au.src = "data:audio/wav;base64," + data.audio_b64;
      au.style.display = "block";
      au.play().catch(() => {});
    } catch (e) {
      msgEl.className = "msg err";
      msgEl.textContent = "❌ " + e.message;
    }
  };
  $("provClearKey").onclick = () => {
    const name = provName();
    if (!name) return;
    if (!confirm(DubI18n.t("o.confirmClear", { name }))) return;
    $("provKey").value = "";
    saveConfig({ tts: { providers: { [name]: { key: null } } } }, $("ttsMsg"));
  };
  $("vmSave").onclick = () => {
    const msgEl = $("vmMsg");
    let obj;
    try {
      obj = JSON.parse($("vmJson").value || "{}");
    } catch (e) {
      msgEl.className = "msg err";
      msgEl.textContent = DubI18n.t("o.jsonErr", { msg: e.message });
      return;
    }
    saveConfig({ tts: { voice_maps: obj } }, msgEl);
  };

  // ------------------------------------------------------------- 插件偏好

  const langSel = $("lang"), hud = $("hud"), uiLangSel = $("uiLang");
  try {
    chrome.storage.local.get({ dub_lang: "zh", dub_hud: true, dub_ui_lang: "" }, (r) => {
      langSel.value = r.dub_lang || "zh";
      hud.checked = r.dub_hud !== false;
      uiLangSel.value = r.dub_ui_lang || "";
    });
  } catch (_) { /* 非扩展上下文打开时保底 */ }
  langSel.onchange = () => chrome.storage.local.set({ dub_lang: langSel.value });
  hud.onchange = () => chrome.storage.local.set({ dub_hud: hud.checked });
  uiLangSel.onchange = () => chrome.storage.local.set({ dub_ui_lang: uiLangSel.value });

  // -------------------------------------------------------------- 任务列表

  const esc = (s) => String(s || "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  function renderJobs(jobs) {
    const tb = $("jobsBody");
    $("jobsEmpty").style.display = jobs.length ? "none" : "block";
    tb.innerHTML = jobs.map((j) => {
      const [key, cls] = STAT[j.status] || ["", ""];
      const txt = key ? DubI18n.t(key) : j.status;
      const name = j.id.length > 8 ? j.id.slice(0, 8) + "…" : j.id;
      const head = j.title
        ? `<b style="font-size:12.5px">${esc(j.title)}</b><div class="hint" style="margin-top:2px"><code>${name}</code></div>`
        : `<code>${name}</code>`;
      const chips = [j.lang, j.tts_backend, j.active ? DubI18n.t("o.session") : ""].filter(Boolean)
        .map((c) => `<span class="badge">${c}</span>`).join(" ");
      const err = j.error ? `<div class="hint">${esc(j.error)}</div>` : "";
      const ops = [];
      if (j.final) ops.push(`<a href="${DubServer.base()}/api/job/${j.id}/audio" target="_blank">${DubI18n.t("o.listen")}</a>`);
      ops.push(`<a href="javascript:void(0)" data-del="${j.id}">${DubI18n.t("o.del")}</a>`);
      return `<tr>
        <td>${head}<div style="margin-top:3px">${chips}</div></td>
        <td><span class="badge ${cls}">${txt}</span>${err}</td>
        <td>${j.chunks_done}/${j.chunks_total || "?"} ${DubI18n.t("o.segs")}${j.segments_total ? `<div class="hint">${j.segments_total} ${DubI18n.t("o.sents")}</div>` : ""}</td>
        <td>${fmtDur(j.duration)}${j.total_s ? `<div class="hint">${DubI18n.t("o.cost", { d: fmtDur(j.total_s) })}</div>` : ""}</td>
        <td>${fmtTime(j.mtime)}</td>
        <td>${ops.join(" · ")}</td>
      </tr>`;
    }).join("");
    tb.querySelectorAll("[data-del]").forEach((a) => {
      a.onclick = () => delJob(a.dataset.del);
    });
  }

  async function delJob(id) {
    const short = id.length > 8 ? id.slice(0, 8) + "…" : id;
    if (!confirm(DubI18n.t("o.confirmDel", { id: short }))) return;
    if (!confirm(DubI18n.t("o.confirmDel2"))) return;
    try {
      const { code, data } = await api(`/api/job/${id}`, { method: "DELETE" });
      if (code !== 200) throw new Error(data.error || code);
    } catch (e) {
      alert(DubI18n.t("o.delFail", { msg: e.message }));
    }
    loadJobs();
  }

  async function loadJobs() {
    try {
      const { data } = await api("/api/jobs");
      renderJobs(data.jobs || []);
    } catch (_) { /* 离线 */ }
  }

  // ------------------------------------------------------------------ 启动

  // 页头引擎地址与离线提示跟随面板设置的端口（DubServer.init 后与变更时都会经 refreshAll 刷新）
  function syncServerText() {
    $("srvUrl").textContent = DubServer.base();
    $("srvPortHint").textContent = DubServer.port;
  }

  async function refreshAll() {
    syncServerText();
    const up = await loadHealth();
    if (up && !$("llmBase").dataset.filled) { await loadConfig(); $("llmBase").dataset.filled = "1"; }
    await loadJobs();
  }
  DubServer.init(refreshAll);
  DubI18n.init(refreshAll); // 语言确定/切换后重刷动态文本（静态文本由 applyDOM 负责）
  setInterval(() => { if (document.visibilityState === "visible") refreshAll(); }, 8000);
})();
