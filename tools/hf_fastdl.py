"""hf_fastdl.py — 多连接并行下载 HF 模型文件，直接补进 huggingface 缓存。

单流下载 ~230KB/s 时 1.6GB 要 100 分钟；16 连接分片约 6 分钟。
用法：
  python tools/hf_fastdl.py <repo_id> <filename> [--workers 16] [--endpoint https://hf-mirror.com]

逻辑：
  1. 定位缓存里的 <blob>.incomplete（没有则从 0 开始）
  2. 并行 Range 分片写入（os.pwrite），失败重试
  3. 校验 sha256 == blob 名（LFS etag 即文件哈希）
  4. 原子改名成正式 blob + 拷贝进 snapshot（Windows 无符号链接权限，hf_hub 在本机就是拷贝模式）
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import os
import queue
import sys
import time

import requests

CHUNK = 16 * 1024 * 1024  # 每片 16MB
PIECE = 256 * 1024        # 流式写入粒度

# Windows 没有 os.pwrite，seek+write 必须成对加锁
import threading

_WLOCK = threading.Lock()


def _pwrite(fd: int, data: bytes, pos: int) -> None:
    with _WLOCK:
        os.lseek(fd, pos, os.SEEK_SET)
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]


def cache_dir() -> str:
    root = os.environ.get("HF_HOME")
    if root:
        return os.path.join(root, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def find_incomplete(model_dir: str, rev: str, filename: str) -> tuple[str, str]:
    """返回 (blob_path_no_ext, incomplete_path 或 '')。"""
    blobs = os.path.join(model_dir, "blobs")
    for name in os.listdir(blobs):
        if name.endswith(".incomplete") and not name.startswith("streams"):
            return os.path.join(blobs, name[: -len(".incomplete")]), os.path.join(blobs, name)
    return "", ""


def head_size(url: str, session: requests.Session) -> tuple[int, str]:
    """不跟随重定向：LFS 文件的真实 sha256 在 X-Linked-ETag，size 在 X-Linked-Size。
    跟到 CDN 拿到的是 xet/CDN 哈希，不是文件 sha256，不能当校验参照。"""
    r = session.head(url, allow_redirects=False, timeout=30)
    r.raise_for_status()
    if "X-Linked-Etag" in r.headers:
        return int(r.headers["X-Linked-Size"]), r.headers["X-Linked-Etag"].strip('"')
    r2 = session.head(url, allow_redirects=True, timeout=30)
    r2.raise_for_status()
    return int(r2.headers["Content-Length"]), r2.headers.get("ETag", "").strip('"')


def download_range(session: requests.Session, url: str, fd: int, a: int, b: int) -> int:
    """下载 [a,b] 写入 fd 偏移 a，返回字节数。带重试。"""
    pos = a
    for attempt in range(6):
        try:
            with session.get(url, headers={"Range": f"bytes={pos}-{b}"}, stream=True,
                             timeout=(15, 30)) as r:
                if r.status_code not in (206,):
                    raise RuntimeError(f"期望 206 得到 {r.status_code}")
                while True:
                    piece = next(r.iter_content(PIECE), None)
                    if not piece:
                        break
                    _pwrite(fd, piece, pos)
                    pos += len(piece)
            if pos == b + 1:
                return pos - a
            raise RuntimeError(f"提前结束 {pos} != {b+1}")
        except Exception as exc:
            if pos > b or attempt == 5:
                raise
            print(f"    [retry] {a//(1<<20)}MB 偏移 {pos}: {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("filename")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"))
    args = ap.parse_args()

    url = f"{args.endpoint.rstrip('/')}/{args.repo}/resolve/main/{args.filename}"
    model_dir = os.path.join(cache_dir(), "models--" + args.repo.replace("/", "--"))
    snap_dir = os.path.join(model_dir, "snapshots")
    rev = open(os.path.join(model_dir, "refs", "main")).read().strip()
    snap_file = os.path.join(snap_dir, rev, args.filename)

    if os.path.exists(snap_file):
        print(f"已存在: {snap_file}")
        return 0

    session = requests.Session()
    total, etag = head_size(url, session)
    print(f"总大小 {total/1e9:.2f} GB, etag {etag[:12]}...")

    blob, inc = find_incomplete(model_dir, rev, args.filename)
    if blob and os.path.basename(blob) != etag:
        print(f"缓存 etag 不匹配（{os.path.basename(blob)[:12]}），从头下载")
        os.remove(inc)
        blob, inc = "", ""
    if not blob:
        blob = os.path.join(model_dir, "blobs", etag)
        inc = blob + ".incomplete"
        open(inc, "wb").close()

    have = os.path.getsize(inc) if os.path.exists(inc) else 0
    nchunk = (total + CHUNK - 1) // CHUNK

    # 分片状态文件：已完成分片序号，支持中断续传（分片乱序完成，不能用前缀判断）
    state = inc + ".chunks"
    if os.path.exists(state):
        # fastdl 自己管过：只信状态文件（稀疏档 size 不可判读）
        finished = {int(x) for x in open(state).read().split() if x.isdigit()}
    else:
        # hf_hub 顺序写入的有效前缀覆盖的分片视为完成
        finished = {i for i in range(nchunk) if (i + 1) * CHUNK <= have}
        if have > total:
            # 疑似被 truncate 过的稀疏坏档，从头来
            os.remove(inc)
            finished = set()

    # 占位到全尺寸才能写任意偏移
    with open(inc, "ab") as f:
        f.truncate(total)
    fd = os.open(inc, os.O_RDWR)

    t0 = time.perf_counter()
    todo: "queue.Queue[int]" = queue.Queue()
    for i in range(nchunk):
        if i not in finished:
            todo.put(i)
    already = sum(min((i + 1) * CHUNK, total) - i * CHUNK for i in finished)
    done = [already]
    sf = open(state, "a")

    def work():
        s = requests.Session()
        while True:
            try:
                i = todo.get_nowait()
            except queue.Empty:
                return
            lo, hi = i * CHUNK, min((i + 1) * CHUNK, total) - 1
            download_range(s, url, fd, lo, hi)
            done[0] += hi - lo + 1
            with _WLOCK:
                sf.write(f"{i}\n")
                sf.flush()
            dt = time.perf_counter() - t0
            rate = (done[0] - already) / max(dt, 1e-9) / 1e6
            print(f"  [{done[0]/1e6:7.0f}/{total/1e6:.0f} MB] {rate:.1f} MB/s", flush=True)

    with cf.ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(lambda _: work(), range(args.workers)))
    os.fsync(fd)
    os.close(fd)

    print("校验 sha256 ...", flush=True)
    h = hashlib.sha256()
    with open(inc, "rb") as f:
        while piece := f.read(8 * 1024 * 1024):
            h.update(piece)
    got = h.hexdigest()
    if got != etag:
        print(f"哈希不匹配: {got}\n期望: {etag}", file=sys.stderr)
        return 1
    os.replace(inc, blob)
    os.remove(state)
    os.makedirs(os.path.dirname(snap_file), exist_ok=True)
    # Windows 无符号链接权限，跟 hf_hub 一样用拷贝
    import shutil
    shutil.copyfile(blob, snap_file)
    print(f"OK {args.repo}/{args.filename} -> {snap_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
