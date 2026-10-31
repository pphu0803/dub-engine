"""dub serve — 本地 HTTP 服务，浏览器插件的后端。只绑 127.0.0.1。

  POST /api/dub           {"url": "...", "referer": "..."} 或 {"path": "本地文件"}
  GET  /api/job/<id>      {status, stage, error, segments, timings}
  GET  /api/job/<id>/audio 成品 mp3（job 完成后）

GPU 串行：同时只跑一个 job，其余 409。所有响应带 CORS + PNA 头
（https 页面经 service worker 访问 http://127.0.0.1 的预检要求）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .pipeline import Options, Runner

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

JOBS: dict[str, dict] = {}
_ACTIVE = threading.Lock()  # 同一时间只允许一个 job 占 GPU


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


def _to_mp3(wav: str, mp3: str) -> None:
    import imageio_ffmpeg

    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
         "-i", wav, "-codec:a", "libmp3lame", "-qscale:a", "2", mp3],
        check=True,
    )


def _run_job(job: dict) -> None:
    with _ACTIVE:
        try:
            o = job["opts"]

            def on_stage(name: str, ev: str, dt: float = 0.0) -> None:
                if ev == "start":
                    job["stage"] = name

            runner = Runner(o, on_stage=on_stage)
            summary = runner.run()
            job["stage"] = "encode"
            wav = os.path.join(runner.work, "final.wav")
            mp3 = wav[:-4] + ".mp3"
            _to_mp3(wav, mp3)
            segs = json.load(open(os.path.join(runner.work, "trans.json"), encoding="utf-8"))
            job.update(
                status="done",
                stage="done",
                audio=mp3,
                duration=summary.get("total_s"),
                segments=[
                    {"start": s["start"], "end": s["end"], "text": s["translated"] or s["text"]}
                    for s in segs
                ],
                timings=summary.get("timings"),
            )
        except Exception as exc:
            job.update(status="error", error=f"{type(exc).__name__}: {exc}")


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
    重签名、B 站过期参数）都复用同一文件——管线缓存键才稳定，刷新页面
    重配音才不会全量重跑。URL 索引让完全相同的 URL 连下载都省掉。"""
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

    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    opts = Options(
        input=src,
        out=os.path.join(out_dir, f"{uuid.uuid4().hex[:8]}.wav"),
        lang=payload.get("lang", "zh"),
        source_lang=payload.get("source_lang", ""),
        asr_backend=payload.get("asr_backend", "transformers"),
        mt_backend="llm",
        tts_backend=payload.get("tts_backend", "edge"),
        chars_per_sec=float(payload.get("chars_per_sec", 4.5)),
        workdir=os.path.join(workdir, "work"),
    )
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "status": "running", "stage": "queued", "src": src, "opts": opts}
    JOBS[jid] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = "dub-serve/0.1"

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
        self._json({"job_id": job["id"], "status": job["status"]})

    def do_GET(self) -> None:
        parts = self.path.strip("/").split("/")
        # /api/job/<id> | /api/job/<id>/audio
        if len(parts) >= 3 and parts[:2] == ["api", "job"]:
            job = JOBS.get(parts[2])
            if not job:
                self._json({"error": "no such job"}, 404)
                return
            if len(parts) == 4 and parts[3] == "audio":
                fp = job.get("audio", "")
                if job["status"] != "done" or not os.path.exists(fp):
                    self._json({"error": "audio not ready"}, 409)
                    return
                body = open(fp, "rb").read()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json({k: job.get(k) for k in
                        ("id", "status", "stage", "error", "segments", "timings", "duration")})
            return
        if self.path == "/api/health":
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:  # 安静
        pass


def serve(port: int = 8765) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[serve] http://127.0.0.1:{port}  (Ctrl+C 退出)")
    httpd.serve_forever()
