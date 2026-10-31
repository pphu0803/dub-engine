/** options.js — 管理页逻辑：引擎状态 / LLM·MiMo 配置（写回 config.json 热生效）/ 插件偏好 / 任务管理。
 * 配置语义与引擎一致：保存时 key 留空 = 保持现值；「清除」= 发 null。 */
(() => {
  const SERVER = "http://127.0.0.1:8765";
  const $ = (id) => document.getElementById(id);
  const STAT = {
    running: ["⏳ 进行中", "run"], done: ["✅ 完成", "done"],
    partial: ["⚠️ 部分完成", "warn"], error: ["❌ 失败", "err"],
  };

  async function api(path, opts = {}, timeoutMs = 8000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(SERVER + path, { signal: ctrl.signal, ...opts });
      return { code: r.status, data: await r.json().catch(() => ({})) };
    } finally {
      clearTimeout(t);
    }
  }

  const fmtDur = (s) => {
    if (!s || !isFinite(s)) return "—";
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m ? `${m}分${r}秒` : `${r}秒`;
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
    $("hUp").textContent = up > 3600 ? `${Math.floor(up / 3600)}时${Math.floor(up % 3600 / 60)}分`
                                     : up > 60 ? `${Math.floor(up / 60)}分${up % 60}秒` : `${up}秒`;
    $("hLlm").textContent = h.llm_set ? (h.llm_model || "已配置") : "未配置";
    $("hLlm").style.color = h.llm_set ? "" : "#e8c268";
    $("hMimo").textContent = h.mimo_set ? "已配置" : "未配置";
    $("hMimo").style.color = h.mimo_set ? "" : "#9a9aa6";
    return true;
  }

  // ----------------------------------------------------------------- 配置

  function fillConfig(cfg) {
    $("llmBase").value = cfg.llm?.base || "";
    $("llmModel").value = cfg.llm?.model || "";
    $("llmKey").placeholder = cfg.llm?.key_set ? `已配置（尾号 ${cfg.llm.key_tail}）——留空不修改` : "未配置";
    $("mimoBase").value = cfg.mimo?.base || "";
    $("mimoKey").placeholder = cfg.mimo?.key_set ? `已配置（尾号 ${cfg.mimo.key_tail}）——留空不修改` : "未配置";
    $("ttsBackend").value = cfg.tts_backend || "edge";
  }

  async function loadConfig() {
    try {
      fillConfig((await api("/api/config")).data);
    } catch (_) { /* 离线时保持占位 */ }
  }

  async function saveConfig(body, msgEl) {
    msgEl.className = "msg";
    msgEl.textContent = "保存中…";
    try {
      const { code, data } = await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (code !== 200) throw new Error(data.error || code);
      fillConfig(data);
      msgEl.className = "msg ok";
      msgEl.textContent = "✅ 已保存并热生效";
      loadHealth();
    } catch (e) {
      msgEl.className = "msg err";
      msgEl.textContent = "❌ " + e.message;
    }
  }

  async function testTarget(target, body, msgEl) {
    msgEl.className = "msg";
    msgEl.textContent = "测试中…（最长 45s）";
    try {
      const { data } = await api("/api/config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, ...body }),
      }, 50000);
      if (!data.ok) throw new Error(data.error || "失败");
      msgEl.className = "msg ok";
      msgEl.textContent = `✅ 连通，${data.latency_ms}ms`;
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
  $("ttsSave").onclick = () => saveConfig({
    tts_backend: $("ttsBackend").value,
    mimo: { base: $("mimoBase").value.trim(), key: $("mimoKey").value },
  }, $("ttsMsg"));
  $("mimoTest").onclick = () => testTarget("mimo", {
    mimo: { base: $("mimoBase").value.trim(), key: $("mimoKey").value },
  }, $("ttsMsg"));
  $("mimoClear").onclick = () => {
    if (!confirm("确定清除已保存的 MiMo key？")) return;
    $("mimoKey").value = "";
    saveConfig({ mimo: { key: null } }, $("ttsMsg"));
  };

  // ------------------------------------------------------------- 插件偏好

  const langSel = $("lang"), hud = $("hud");
  try {
    chrome.storage.local.get({ dub_lang: "zh", dub_hud: true }, (r) => {
      langSel.value = r.dub_lang || "zh";
      hud.checked = r.dub_hud !== false;
    });
  } catch (_) { /* 非扩展上下文打开时保底 */ }
  langSel.onchange = () => chrome.storage.local.set({ dub_lang: langSel.value });
  hud.onchange = () => chrome.storage.local.set({ dub_hud: hud.checked });

  // -------------------------------------------------------------- 任务列表

  function renderJobs(jobs) {
    const tb = $("jobsBody");
    $("jobsEmpty").style.display = jobs.length ? "none" : "block";
    tb.innerHTML = jobs.map((j) => {
      const [txt, cls] = STAT[j.status] || [j.status, ""];
      const name = j.id.length > 8 ? j.id.slice(0, 8) + "…" : j.id;
      const chips = [j.lang, j.tts_backend, j.active ? "本次会话" : ""].filter(Boolean)
        .map((c) => `<span class="badge">${c}</span>`).join(" ");
      const err = j.error ? `<div class="hint">${j.error}</div>` : "";
      const ops = [];
      if (j.final) ops.push(`<a href="${SERVER}/api/job/${j.id}/audio" target="_blank">试听</a>`);
      ops.push(`<a href="javascript:void(0)" data-del="${j.id}">删除</a>`);
      return `<tr>
        <td><code>${name}</code><div style="margin-top:3px">${chips}</div></td>
        <td><span class="badge ${cls}">${txt}</span>${err}</td>
        <td>${j.chunks_done}/${j.chunks_total || "?"} 段${j.segments_total ? `<div class="hint">${j.segments_total} 句</div>` : ""}</td>
        <td>${fmtDur(j.duration)}${j.total_s ? `<div class="hint">耗时 ${fmtDur(j.total_s)}</div>` : ""}</td>
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
    if (!confirm(`删除任务 ${short}？\n该任务的成品音频与中间产物会一并删除，不可恢复。`)) return;
    if (!confirm("再次确认：真的删除？")) return;
    try {
      const { code, data } = await api(`/api/job/${id}`, { method: "DELETE" });
      if (code !== 200) throw new Error(data.error || code);
    } catch (e) {
      alert("删除失败：" + e.message);
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

  async function refreshAll() {
    const up = await loadHealth();
    if (up && !$("llmBase").dataset.filled) { await loadConfig(); $("llmBase").dataset.filled = "1"; }
    await loadJobs();
  }
  refreshAll();
  setInterval(() => { if (document.visibilityState === "visible") refreshAll(); }, 8000);
})();
