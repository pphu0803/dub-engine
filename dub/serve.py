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


def _download(url: str, referer: str, dst: str) -> int:
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    if referer:
        s.headers["Referer"] = referer
    r = s.get(url, stream=True, timeout=(15, 60))
    r.raise_for_status()
    n = 0
    with open(dst, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
            n += len(chunk)
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


def start_job(payload: dict, workdir: str) -> dict:
    if "path" in payload:
        src = os.path.abspath(payload["path"])
        if not os.path.exists(src):
            raise FileNotFoundError(src)
    else:
        url = payload.get("url", "")
        if not url:
            raise ValueError("需要 url 或 path")
        ext = ".m4s"
        base = os.path.splitext(os.path.basename(urlparse(url).path))[0][:60] or "dl"
        dldir = os.path.join(workdir, "dl")
        os.makedirs(dldir, exist_ok=True)
        src = os.path.join(dldir, f"{base}_{uuid.uuid4().hex[:6]}{ext}")
        n = _download(url, payload.get("referer", ""), src)
        print(f"[serve] 音轨下载 {n/1e6:.1f} MB -> {src}")

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
