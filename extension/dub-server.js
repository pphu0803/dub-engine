/**
 * dub-server.js — 引擎基址统一管理（插件 v0.3.2 起）
 *
 * 端口存 chrome.storage.local.dub_port（默认 8765），面板「引擎端口」可改，
 * 改动即对下一次请求生效——不再需要改 content.js 源码换端口。
 * popup/options 以 <script> 引入本文件；content.js 经 manifest 的
 * content_scripts 同批注入（同一隔离世界，顶层 const 跨文件可见）。
 */
const DubServer = {
  port: 8765,
  base() { return `http://127.0.0.1:${this.port}`; },
  /**
   * @param {Function} [onChange] 首次读到端口后与每次端口变更时回调
   *   （调用方借此重刷状态灯/提示语）。
   */
  init(onChange) {
    const apply = (v) => {
      const p = parseInt(v, 10);
      if (p >= 1 && p <= 65535) this.port = p;
    };
    try {
      chrome.storage.local.get({ dub_port: 8765 }, (r) => {
        apply(r.dub_port);
        if (onChange) onChange();
      });
      chrome.storage.onChanged.addListener((ch, area) => {
        if (area !== "local" || !("dub_port" in ch)) return;
        const prev = this.port;
        apply(ch.dub_port.newValue);
        if (this.port !== prev && onChange) onChange();
      });
    } catch (_) { /* 无扩展上下文（如直接以文件打开）时保持默认端口 */ }
  },
};
