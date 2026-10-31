"""dub serve — 本地 HTTP 服务，浏览器插件的后端。只绑 127.0.0.1。

  POST /api/dub           {"url": "...", "referer": "..."} 或 {"path": "本地文件"}
  GET  /api/job/<id>            {status, stage, chunks[], duration, error}
  GET  /api/job/<id>/audio/<k>  第 k 段 mp3（该段 done 即可取）
  GET  /api/job/<id>/audio      整轨 mp3（全部完成后）

流水线为分块流式（dub/streaming.py）：段 0 最先出声，后台顺序推进；
任务目录按 内容哈希+语言+后端 命名，重复请求同一内容直接秒回。
GPU 串行：同时只跑一个 job，其余排队。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import streaming

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

JOBS: dict[str, dict] = {}
JOBS_BY_KEY: dict[str, str] = {}
_ACTIVE = threading.Lock()  # 同一时间只允许一个 job 占 GPU
LLM_CFG = {"base": "", "key": "", "model": ""}


def _load_llm_config(cli: dict) -> dict:
    """LLM 配置三级优先：CLI 参数 > 环境变量 > config.json（不入库）。"""
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = {}
    p = os.path.join(workdir, "config.json")
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8")).get("llm", {}) or {}
        except Exception:
            cfg = {}
    merged = {}
    for k, env_k in (("base", "DUB_LLM_BASE"), ("key", "DUB_LLM_KEY"), ("model", "DUB_LLM_MODEL")):
        merged[k] = (cli.get(k) or os.environ.get(env_k, "") or str(cfg.get(k, ""))).strip()
    return merged


def _looks_like_audio(head: bytes) -> bool:
    if head[:4] in (b"OggS", b"RIFF", b"fLaC") or head[:3] == b"ID3":
        return True
    if head[4:8] == b"ftyp":  # mp4/m4s/m4a 容器
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:  # mpeg audio
        return True
    return False


def _download(url: str, referer: str, dst: str) -> int:
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    if referer:
        s.headers["Referer"] = referer
    r = s.get(url, stream=True, timeout=(15, 60))
    r.raise_for_status()
    n = 0
    head = b""
    with open(dst, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            if len(head) < 16:
                head += chunk
            f.write(chunk)
            n += len(chunk)
    # 插件偶发抓到 HTML/空响应（播放器杂项请求），在这里拦下给清楚报错
    if n < 64 * 1024 or not _looks_like_audio(head[:16]):
        ctype = r.headers.get("content-type", "?")
        os.remove(dst)
        raise ValueError(
            f"捕获的 URL 不是音轨（{n/1024:.0f} KB, {ctype}）。刷新视频页、播放 2 秒后重试"
        )
    return n


def _content_key(fp: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(fp, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()[:16]


def _fetch_audio(url: str, referer: str, workdir: str) -> str:
    """下载音轨并按内容哈希落盘。同一内容无论 URL 怎么变（YT 每次加载
    重签名、B 站过期参数）都复用同一文件——任务缓存键才稳定。URL 索引
    让完全相同的 URL 连下载都省掉。"""
    dldir = os.path.join(workdir, "dl")
    os.makedirs(dldir, exist_ok=True)
    idx_path = os.path.join(dldir, "dl_index.json")
    try:
        idx = json.load(open(idx_path, encoding="utf-8"))
    except Exception:
        idx = {}
    if idx.get(url) and os.path.exists(idx[url]):
        print(f"[serve] 音轨命中下载缓存: {idx[url]}")
        return idx[url]

    ext = os.path.splitext(urlparse(url).path)[1] or ".m4s"
    if not re.match(r"^\.[a-z0-9]{2,5}$", ext, re.I):
        ext = ".m4s"
    tmp = os.path.join(dldir, f"tmp_{uuid.uuid4().hex[:8]}{ext}")
    n = _download(url, referer, tmp)
    ck = _content_key(tmp)
    final = os.path.join(dldir, f"{ck}{ext}")
    if os.path.exists(final):
        os.remove(tmp)
    else:
        os.replace(tmp, final)
    idx[url] = final
    json.dump(idx, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[serve] 音轨下载 {n/1e6:.1f} MB -> {final} (content {ck})")
    return final


def start_job(payload: dict, workdir: str) -> dict:
    if "path" in payload:
        src = os.path.abspath(payload["path"])
        if not os.path.exists(src):
            raise FileNotFoundError(src)
    else:
        url = payload.get("url", "")
        if not url:
            raise ValueError("需要 url 或 path")
        src = _fetch_audio(url, payload.get("referer", ""), workdir)

    lang = payload.get("lang", "zh")
    tts_backend = payload.get("tts_backend", "edge")
    # PIPELINE_VER：对齐/混音逻辑升级时 +1，让"重新配音"生成新任务吃到新逻辑
    key = f"{os.path.splitext(os.path.basename(src))[0]}_{lang}_{tts_backend}_p3"

    if key in JOBS_BY_KEY:
        job = JOBS[JOBS_BY_KEY[key]]
        if job["status"] == "done":
            return {"job_id": job["id"], "status": "done", "cached": True,
                    "chunks_done": job.get("chunks_done", 0)}
        if job["status"] in ("error", "partial"):
            # 失败/部分完成的任务：再点一次=续跑失败段（state 驱动）
            job["status"] = "running"
            threading.Thread(target=job["run"], daemon=True).start()
            return {"job_id": job["id"], "status": "running", "cached": False,
                    "chunks_done": job.get("chunks_done", 0)}
        return {"job_id": job["id"], "status": job["status"],
                "cached": False, "chunks_done": job.get("chunks_done", 0)}

    jid = uuid.uuid4().hex[:8]
    job = {
        "id": jid, "key": key, "status": "running", "stage": "queued",
        "chunks": [], "chunks_done": 0, "src": src, "lang": lang,
        "source_lang": payload.get("source_lang", ""),
        "tts_backend": tts_backend,
    }
    JOBS[jid] = job
    JOBS_BY_KEY[key] = jid

    def run() -> None:
        with _ACTIVE:
            try:
                streaming.run_job(
                    job, src, lang, job["source_lang"], tts_backend, workdir,
                    api_base=LLM_CFG["base"],
                    api_key=LLM_CFG["key"],
                    llm_model=LLM_CFG["model"],
                )
            except Exception as exc:
                job["status"] = "error"
                job["error"] = f"{type(exc).__name__}: {exc}"

    job["run"] = run
    threading.Thread(target=run, daemon=True).start()
    return {"job_id": jid, "status": job["status"], "cached": False, "chunks_done": 0}


class Handler(BaseHTTPRequestHandler):
    server_version = "dub-serve/0.2"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, fp: str, mime: str) -> None:
        body = open(fp, "rb").read()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/api/dub":
            self._json({"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
            workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            job = start_job(payload, workdir)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return
        self._json(job)

    def do_GET(self) -> None:
        parts = self.path.strip("/").split("/")
        if self.path == "/api/health":
            self._json({"ok": True})
            return
        # /api/job/<id> | /api/job/<id>/audio | /api/job/<id>/audio/<k>
        if len(parts) >= 3 and parts[:2] == ["api", "job"]:
            job = JOBS.get(parts[2])
            if not job:
                self._json({"error": "no such job"}, 404)
                return
            if len(parts) == 4 and parts[3] == "audio":
                fp = os.path.join(job.get("dir", ""), "final.mp3")
                if job["status"] != "done" or not os.path.exists(fp):
                    self._json({"error": "final not ready"}, 409)
                    return
                self._file(fp, "audio/mpeg")
                return
            if len(parts) == 5 and parts[3] == "audio":
                chunks = job.get("chunks") or []
                k = int(parts[4])
                ch = chunks[k] if k < len(chunks) else {}
                fp = ch.get("mp3", "")
                if ch.get("status") != "done" or not fp or not os.path.exists(fp):
                    self._json({"error": "chunk not ready"}, 409)
                    return
                try:
                    self._file(fp, "audio/mpeg")
                except OSError:
                    self._json({"error": "chunk file missing"}, 410)
                return
            self._json({
                "id": job["id"], "status": job["status"], "stage": job.get("stage"),
                "error": job.get("error"), "duration": job.get("duration"),
                "chunks": [
                    {"i": c["i"], "start": round(c["start"], 2), "end": round(c["end"], 2),
                     "status": c["status"]}
                    for c in (job.get("chunks") or [])
                ],
                "total_s": job.get("total_s"), "segments_total": job.get("segments_total"),
            })
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:  # 安静
        pass


def serve(port: int = 8765, llm_base: str = "", llm_key: str = "", llm_model: str = "") -> None:
    global LLM_CFG
    LLM_CFG = _load_llm_config({"base": llm_base, "key": llm_key, "model": llm_model})
    if LLM_CFG["base"] and LLM_CFG["key"]:
        print(f"[serve] LLM: {LLM_CFG['model'] or '(默认模型)'} @ {LLM_CFG['base']}")
    else:
        print("[serve] 未配置 LLM（config.json / 环境变量 / --llm-* 三选一），翻译将不可用")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[serve] http://127.0.0.1:{port}  (Ctrl+C 退出)")
    httpd.serve_forever()
