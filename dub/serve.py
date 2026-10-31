"""dub serve — 本地 HTTP 服务，浏览器插件的后端。只绑 127.0.0.1。

  POST /api/dub           {"url": "...", "referer": "..."} 或 {"path": "本地文件"}
  GET  /api/job/<id>            {status, stage, chunks[], duration, error}
  GET  /api/job/<id>/audio/<k>  第 k 段 mp3（该段 done 即可取）
  GET  /api/job/<id>/audio      整轨 mp3（全部完成后）
  GET  /api/health              引擎状态（插件面板）
  GET  /api/config              读配置（key 打码）
  POST /api/config             写 config.json + 内存热生效（不用重启）
  POST /api/config/test        配置测活 {"target": "llm"|"mimo", ...覆盖值}
  GET  /api/jobs                任务列表（内存 + 磁盘历史）
  DELETE /api/job/<id>         删除任务目录

config/jobs/delete 类端点校验 Origin：只放行 chrome-extension:// 与无
Origin（curl），网页 Origin 一律 403——任意网页不该能改配置、列任务。
serve 重启后内存任务表清空，/api/job/<目录名> 会按 state.json 重建只读
视图（轮询/取音频/删除照常可用）。

流水线为分块流式（dub/streaming.py）：段 0 最先出声，后台顺序推进；
任务目录按 内容哈希+语言+后端 命名，重复请求同一内容直接秒回。
GPU 串行：同时只跑一个 job，其余排队。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
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
MIMO_CFG = {"base": "", "key": ""}
TTS_DEFAULT = "edge"

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAM_DIR = os.path.join(WORKDIR, "work", "stream")
CFG_PATH = os.path.join(WORKDIR, "config.json")
_START_TS = time.time()
_CFG_LOCK = threading.Lock()  # config.json 读写互斥
# 磁盘历史任务 id = 任务目录名（内容哈希_语言_后端_流水线版）；顺带挡路径注入
_JOBID_RE = re.compile(r"^[0-9a-f]{16}_[a-z]{2}_[a-z0-9]+_p[0-9a-z]+$")


def _load_config(section: str, cli: dict, env_map: dict) -> dict:
    """配置三级优先：CLI 参数 > 环境变量 > config.json 的 section（不入库）。"""
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = {}
    p = os.path.join(workdir, "config.json")
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8")).get(section, {}) or {}
        except Exception:
            cfg = {}
    merged = {}
    for k, env_k in env_map.items():
        merged[k] = (cli.get(k) or os.environ.get(env_k, "") or str(cfg.get(k, ""))).strip()
    return merged


def _load_llm_config(cli: dict) -> dict:
    return _load_config("llm", cli,
                        {"base": "DUB_LLM_BASE", "key": "DUB_LLM_KEY", "model": "DUB_LLM_MODEL"})


def _load_mimo_config(cli: dict) -> dict:
    cfg = _load_config("mimo", cli, {"base": "DUB_MIMO_BASE", "key": "DUB_MIMO_KEY"})
    return {"base": cfg.get("base") or "", "key": cfg.get("key") or ""}


def _load_tts_default(cli_val: str) -> str:
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    val = ""
    p = os.path.join(workdir, "config.json")
    if os.path.exists(p):
        try:
            val = str(json.load(open(p, encoding="utf-8")).get("tts_backend", "") or "")
        except Exception:
            val = ""
    return (cli_val or os.environ.get("DUB_TTS_BACKEND", "") or val or "edge").strip()


# ---------------------------------------------------------------- 配置读写/测活

def _masked(sec: dict) -> dict:
    key = str(sec.get("key", "") or "")
    return {"base": sec.get("base", ""), "model": sec.get("model", ""),
            "key_set": bool(key), "key_tail": key[-4:] if key else ""}


def _config_view() -> dict:
    return {"tts_backend": TTS_DEFAULT, "pipeline_ver": streaming.PIPELINE_VER,
            "llm": _masked(LLM_CFG), "mimo": _masked(MIMO_CFG)}


def _update_config(body: dict) -> None:
    """写 config.json + 热更新内存配置。字段语义：缺失或 "" = 保留现值，
    null = 清除（管理页的 key 输入框留空即"不修改"，防止误清）。"""
    with _CFG_LOCK:
        try:
            cfg = json.load(open(CFG_PATH, encoding="utf-8"))
        except Exception:
            cfg = {}

        def merge(sec_name: str, fields: dict, updates: dict) -> None:
            sec = cfg.setdefault(sec_name, {})
            for k in fields:
                if k not in updates:
                    continue
                v = updates[k]
                if v is None:
                    sec[k] = ""
                elif isinstance(v, str) and v.strip():
                    sec[k] = v.strip()

        merge("llm", ("base", "key", "model"), body.get("llm") or {})
        merge("mimo", ("base", "key"), body.get("mimo") or {})
        if body.get("tts_backend") in ("edge", "mimo"):
            cfg["tts_backend"] = body["tts_backend"]
        json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 写盘成功后再热生效（_load_* 的优先级与启动时一致：环境变量 > config.json）
    global LLM_CFG, MIMO_CFG, TTS_DEFAULT
    LLM_CFG = _load_llm_config({})
    MIMO_CFG = _load_mimo_config({})
    TTS_DEFAULT = _load_tts_default("")


def _test_llm(base: str, key: str, model: str) -> dict:
    import requests

    if not (base and key and model):
        return {"ok": False, "error": "缺少 base / key / model"}
    sess = requests.Session()
    sess.trust_env = False  # 与翻译管线同款直连
    t0 = time.perf_counter()
    try:
        r = sess.post(base.rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "temperature": 0, "max_tokens": 1,
                            "messages": [{"role": "user", "content": "hi"}]},
                      timeout=(10, 30))
        r.raise_for_status()
        r.json()
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _test_mimo(base: str, key: str) -> dict:
    import tempfile

    from . import mimo

    if not key:
        return {"ok": False, "error": "缺少 key"}
    fd, dst = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    t0 = time.perf_counter()
    try:
        mimo.tts_one(base or mimo.DEFAULT_BASE, key, "你好，这是配音引擎测试。",
                     "mimo_default", dst, timeout=45)
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass


# ---------------------------------------------------------------- 任务列表/重建/删除

def _job_status_of(chunks: list, d: str) -> tuple[str, str]:
    """state.json 只存段状态，任务级状态按磁盘现场推断（final.mp3 是最硬的证据）。"""
    if os.path.exists(os.path.join(d, "final.mp3")):
        return "done", ""
    bad = sum(1 for c in chunks if c.get("status") == "error")
    if bad:
        return "partial", f"{bad} 段失败，重新配音可续跑"
    if any(c.get("status") == "done" for c in chunks):
        return "partial", "任务曾中断，重新配音可续跑"
    return "error", "任务曾中断（serve 重启），重新配音续跑"


def _disk_job_row(name: str) -> dict | None:
    if not _JOBID_RE.match(name):
        return None
    d = os.path.join(STREAM_DIR, name)
    sp = os.path.join(d, "state.json")
    if not os.path.exists(sp):
        return None
    try:
        state = json.load(open(sp, encoding="utf-8"))
    except Exception:
        return None
    chunks = state.get("chunks") or []
    status, note = _job_status_of(chunks, d)
    parts = name.split("_")
    return {
        "id": name, "active": False, "status": status,
        "lang": parts[1] if len(parts) >= 4 else "",
        "tts_backend": parts[2] if len(parts) >= 4 else "",
        "chunks_done": sum(1 for c in chunks if c.get("status") == "done"),
        "chunks_total": len(chunks),
        "duration": round(chunks[-1]["end"], 1) if chunks else 0,
        "segments_total": sum(len(c.get("segments") or []) for c in chunks),
        "error": note, "final": status == "done",
        "mtime": int(os.path.getmtime(sp)),
    }


def _jobs_view() -> list[dict]:
    rows = []
    seen: set[str] = set()
    for job in JOBS.values():
        d = job.get("dir") or ""
        name = os.path.basename(d) if d else ""
        if name:
            seen.add(name)
        sp = os.path.join(d, "state.json") if d else ""
        rows.append({
            "id": job["id"], "active": True, "status": job["status"],
            "stage": job.get("stage"), "lang": job.get("lang", ""),
            "tts_backend": job.get("tts_backend", ""),
            "chunks_done": job.get("chunks_done", 0),
            "chunks_total": len(job.get("chunks") or []),
            "duration": job.get("duration"), "total_s": job.get("total_s"),
            "error": job.get("error"), "final": job["status"] == "done",
            "mtime": int(os.path.getmtime(sp)) if sp and os.path.exists(sp) else int(time.time()),
        })
    try:
        names = os.listdir(STREAM_DIR)
    except OSError:
        names = []
    disk = [r for r in (_disk_job_row(n) for n in names) if r and r["id"] not in seen]
    disk.sort(key=lambda r: r["mtime"], reverse=True)
    return rows + disk[:30]


def _rehydrate(jid: str) -> dict | None:
    """serve 重启后内存任务表清空：按目录名从 state.json 重建只读视图，
    轮询/取段音频/删除照常可用。真正续跑走 POST /api/dub（确定性目录名命中）。"""
    if not _JOBID_RE.match(jid):
        return None
    d = os.path.join(STREAM_DIR, jid)
    sp = os.path.join(d, "state.json")
    if not os.path.exists(sp):
        return None
    try:
        state = json.load(open(sp, encoding="utf-8"))
    except Exception:
        return None
    chunks = state.get("chunks") or []
    if not chunks:
        return None
    status, note = _job_status_of(chunks, d)
    parts = jid.split("_")
    return {
        "id": jid, "dir": d, "status": status, "stage": status,
        "error": note or None, "duration": chunks[-1]["end"],
        "lang": parts[1] if len(parts) >= 4 else "",
        "tts_backend": parts[2] if len(parts) >= 4 else "",
        "chunks": chunks,
        "chunks_done": sum(1 for c in chunks if c.get("status") == "done"),
    }


def _delete_job(jid: str) -> tuple[dict, int]:
    job = JOBS.get(jid)
    d = ""
    if job:
        if job["status"] == "running":
            return {"error": "任务进行中，等完成或失败后再删"}, 409
        d = job.get("dir") or ""
        JOBS.pop(jid, None)
        if JOBS_BY_KEY.get(job.get("key")) == jid:
            JOBS_BY_KEY.pop(job.get("key"), None)
    else:
        if not _JOBID_RE.match(jid):
            return {"error": "no such job"}, 404
        d = os.path.join(STREAM_DIR, jid)
        if not os.path.isdir(d):
            return {"error": "no such job"}, 404
    if d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "removed": os.path.basename(d)}, 200


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
    n = 0
    head = b""
    last_exc: Exception | None = None
    for attempt in range(3):
        # B 站 CDN 偶发 TLS 重置（SSL UNEXPECTED_EOF）/连接抖动，整段下载三连重试
        try:
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
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 2:
                raise
            print(f"[serve] 下载重试 {attempt + 1}/3：{type(exc).__name__}", flush=True)
            import time as _t

            _t.sleep(2 * (attempt + 1))
    assert last_exc is None
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
    tts_backend = payload.get("tts_backend") or TTS_DEFAULT
    # 任务键与状态目录共用 PIPELINE_VER（streaming.py），升级对齐逻辑时 +1，
    # 让"重新配音"生成新任务吃到新逻辑
    key = (f"{os.path.splitext(os.path.basename(src))[0]}_{lang}_{tts_backend}"
           f"_{streaming.PIPELINE_VER}")

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
                    mimo_base=MIMO_CFG["base"],
                    mimo_key=MIMO_CFG["key"],
                )
            except Exception as exc:
                job["status"] = "error"
                job["error"] = f"{type(exc).__name__}: {exc}"

    job["run"] = run
    threading.Thread(target=run, daemon=True).start()
    return {"job_id": jid, "status": job["status"], "cached": False, "chunks_done": 0}


class Handler(BaseHTTPRequestHandler):
    server_version = "dub-serve/0.3"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _origin_ok(self) -> bool:
        """配置/任务管理端点的来源闸门：content script 的 fetch 带网页 Origin
        （它们只用 /api/dub 与 /api/job），管理操作只应来自扩展页面——带网页
        Origin 的一律拒绝；无 Origin（curl）放行方便调试。"""
        o = self.headers.get("Origin", "")
        return not o or o.startswith("chrome-extension://")

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
        if self.path == "/api/config":
            if not self._origin_ok():
                self._json({"error": "forbidden"}, 403)
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                _update_config(body)
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
                return
            self._json(_config_view())
            return
        if self.path == "/api/config/test":
            if not self._origin_ok():
                self._json({"error": "forbidden"}, 403)
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
                return
            if body.get("target") == "llm":
                o = body.get("llm") or {}
                self._json(_test_llm(o.get("base") or LLM_CFG["base"],
                                     o.get("key") or LLM_CFG["key"],
                                     o.get("model") or LLM_CFG["model"]))
            elif body.get("target") == "mimo":
                o = body.get("mimo") or {}
                self._json(_test_mimo(o.get("base") or MIMO_CFG["base"],
                                      o.get("key") or MIMO_CFG["key"]))
            else:
                self._json({"error": "target 须为 llm 或 mimo"}, 400)
            return
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

    def do_DELETE(self) -> None:
        if not self._origin_ok():
            self._json({"error": "forbidden"}, 403)
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "job"]:
            body, code = _delete_job(parts[2])
            self._json(body, code)
            return
        self._json({"error": "not found"}, 404)

    def do_GET(self) -> None:
        path, _, qs = self.path.partition("?")
        parts = path.strip("/").split("/")
        if path == "/api/health":
            running = sum(1 for j in JOBS.values() if j["status"] == "running")
            self._json({
                "ok": True, "version": "0.3", "pipeline_ver": streaming.PIPELINE_VER,
                "tts_backend": TTS_DEFAULT,
                "llm_set": bool(LLM_CFG["base"] and LLM_CFG["key"]),
                "llm_model": LLM_CFG["model"],
                "mimo_set": bool(MIMO_CFG["key"]),
                "jobs": {"running": running, "total": len(JOBS)},
                "uptime_s": int(time.time() - _START_TS),
            })
            return
        if path == "/api/config":
            if not self._origin_ok():
                self._json({"error": "forbidden"}, 403)
                return
            self._json(_config_view())
            return
        if path == "/api/jobs":
            if not self._origin_ok():
                self._json({"error": "forbidden"}, 403)
                return
            self._json({"jobs": _jobs_view()})
            return
        # /api/job/<id> | /api/job/<id>/audio | /api/job/<id>/audio/<k>
        if len(parts) >= 3 and parts[:2] == ["api", "job"]:
            job = JOBS.get(parts[2]) or _rehydrate(parts[2])
            if not job:
                self._json({"error": "no such job"}, 404)
                return
            # 轮询捎带播放位置（?t=秒）→ 驱动"离播放头最近的段先配"调度
            m = re.search(r"(?:^|&)t=([0-9.]+)", qs)
            if m:
                try:
                    job["pos"] = max(0.0, float(m.group(1)))
                except ValueError:
                    pass
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
                "lang": job.get("lang", ""), "tts_backend": job.get("tts_backend", ""),
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


def serve(port: int = 8765, llm_base: str = "", llm_key: str = "", llm_model: str = "",
          mimo_base: str = "", mimo_key: str = "", tts_backend: str = "") -> None:
    global LLM_CFG, MIMO_CFG, TTS_DEFAULT, _START_TS
    _START_TS = time.time()
    LLM_CFG = _load_llm_config({"base": llm_base, "key": llm_key, "model": llm_model})
    if LLM_CFG["base"] and LLM_CFG["key"]:
        print(f"[serve] LLM: {LLM_CFG['model'] or '(默认模型)'} @ {LLM_CFG['base']}")
    else:
        print("[serve] 未配置 LLM（config.json / 环境变量 / --llm-* 三选一，或到插件管理页填），翻译将不可用")
    MIMO_CFG = _load_mimo_config({"base": mimo_base, "key": mimo_key})
    if MIMO_CFG["key"]:
        print(f"[serve] MiMo TTS: {MIMO_CFG['base'] or '(默认端点)'}")
    TTS_DEFAULT = _load_tts_default(tts_backend)
    print(f"[serve] 默认 TTS: {TTS_DEFAULT}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[serve] http://127.0.0.1:{port}  (Ctrl+C 退出；配置可在插件管理页改，热生效)")
    httpd.serve_forever()
