#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_media.py — 测量"网页视频的音轨能多快拿到"

这是整个配音工具的第一块地基：如果拿不到音轨，或者拿得比播放还慢，
后面的预取架构就不成立。所以先只做一件事——把音轨拉下来，报速度。

支持：
  * HLS  (m3u8：master / media playlist / EXT-X-BYTERANGE / EXT-X-MAP)
  * DASH (mpd：audio AdaptationSet / SegmentTemplate / SegmentTimeline / SegmentList)
  * 直链 (mp4 / m4s / m4a，限速读取测吞吐)

用法：
  python tools/probe_media.py --url "<音视频地址>" [--referer ...] [--cookie ...]

示例：
  python tools/probe_media.py --url "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
  python tools/probe_media.py --url "https://.../audio.m4s" --referer "https://www.bilibili.com"

产物落在 data/ 下；本工具不含任何站点适配器。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #


@dataclass
class Seg:
    url: str
    byte_range: Optional[tuple[int, int]] = None  # 闭区间 (start, end)
    duration: float = 0.0


@dataclass
class Report:
    kind: str = "unknown"
    source: str = ""
    picked: str = ""
    segments: int = 0
    bytes: int = 0
    wall_s: float = 0.0
    media_duration_s: float = 0.0
    encrypted: bool = False
    out_path: str = ""
    decode: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def mbps(self) -> float:
        return self.bytes / 1e6 / self.wall_s if self.wall_s > 0 else 0.0

    @property
    def speed_x(self) -> float:
        return self.media_duration_s / self.wall_s if self.wall_s > 0 else 0.0

    @property
    def kbps(self) -> float:
        return self.bytes * 8 / 1000 / self.media_duration_s if self.media_duration_s else 0.0

    def summary(self) -> str:
        L = [
            "",
            "=" * 64,
            f"类型        : {self.kind}",
            f"选中        : {self.picked}",
            f"分片数      : {self.segments}",
            f"下载字节    : {self.bytes/1e6:.2f} MB",
            f"耗时        : {self.wall_s:.2f} s",
            f"吞吐        : {self.mbps:.1f} MB/s",
        ]
        if self.media_duration_s:
            L.append(f"媒体时长    : {self.media_duration_s:.1f} s（码率约 {self.kbps:.0f} kbps）")
        if self.speed_x:
            L.append(f"倍率        : {self.speed_x:.1f}× 实时")
            L.append(f"10 分钟音频 : 约 {600/self.speed_x:.1f} s")
        if self.encrypted:
            L.append("加密        : 是（仅测了下载，未解密）")
        L.append(f"解码检查    : {self.decode}")
        if self.out_path:
            L.append(f"落盘        : {self.out_path}")
        for n in self.notes:
            L.append(f"备注        : {n}")
        L.append("=" * 64)
        return "\n".join(L)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def make_session(args) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": args.user_agent or UA, "Accept": "*/*"})
    if args.referer:
        s.headers["Referer"] = args.referer
    if args.cookie:
        s.headers["Cookie"] = args.cookie
    s.verify = not args.insecure
    return s


def get_text(session: requests.Session, url: str, timeout: float) -> tuple[str, str]:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text, r.url


def parse_attrs(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', s):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def safe_name(url: str, suffix: str) -> str:
    host = re.sub(r"[^A-Za-z0-9._-]", "_", urlparse(url).netloc)
    h = abs(hash(url)) % (10**8)
    return f"{host}_{h}{suffix}"


def parse_byterange(v: str, prev_end: Optional[int]) -> tuple[int, int]:
    v = v.strip()
    if "@" in v:
        n, o = v.split("@", 1)
        n, o = int(n), int(o)
    else:
        n = int(v)
        o = prev_end if prev_end is not None else 0
    return o, o + n - 1


# --------------------------------------------------------------------------- #
# HLS
# --------------------------------------------------------------------------- #


def hls_is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text or "#EXT-X-MEDIA" in text


def hls_parse_master(text: str):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    medias, variants = [], []
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-MEDIA:"):
            medias.append(parse_attrs(ln.split(":", 1)[1]))
        elif ln.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attrs(ln.split(":", 1)[1])
            uri = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].startswith("#") else None
            if uri:
                variants.append((attrs, uri))
    return medias, variants


def hls_pick(master_text: str, master_url: str) -> tuple[str, str]:
    medias, variants = hls_parse_master(master_text)
    audio = [m for m in medias if m.get("TYPE") == "AUDIO" and m.get("URI")]
    if audio:
        # 优先双声道（CHANNELS=2），排除 5.1/AC-3 这类体积大的
        def score(m):
            ch = m.get("CHANNELS", "2")
            return (ch != "2", m.get("DEFAULT") != "YES")
        audio.sort(key=score)
        m = audio[0]
        return (
            urljoin(master_url, m["URI"]),
            f'音频轨 "{m.get("NAME", "?")}" lang={m.get("LANGUAGE", "?")} ch={m.get("CHANNELS", "?")}',
        )
    if variants:
        variants.sort(key=lambda v: int(v[0].get("BANDWIDTH", "0") or 0))
        attrs, uri = variants[0]
        return (
            urljoin(master_url, uri),
            f'最低码率变体 BANDWIDTH={attrs.get("BANDWIDTH", "?")}（含视频，权宜之计）',
        )
    return master_url, "已是 media playlist"


def hls_parse_media(text: str, base_url: str):
    segs: list[Seg] = []
    init_seg: Optional[Seg] = None
    duration = 0.0
    encrypted = False
    cur_dur = 0.0
    pending_range: Optional[tuple[int, int]] = None
    prev_end: Optional[int] = None

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#EXTINF:"):
            try:
                cur_dur = float(ln.split(":", 1)[1].split(",")[0])
            except ValueError:
                cur_dur = 0.0
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            pending_range = parse_byterange(ln.split(":", 1)[1], prev_end)
        elif ln.startswith("#EXT-X-MAP:"):
            a = parse_attrs(ln.split(":", 1)[1])
            if a.get("URI"):
                br = parse_byterange(a["BYTERANGE"], None) if a.get("BYTERANGE") else None
                init_seg = Seg(urljoin(base_url, a["URI"]), br, 0.0)
        elif ln.startswith("#EXT-X-KEY:"):
            a = parse_attrs(ln.split(":", 1)[1])
            if a.get("METHOD", "NONE") != "NONE":
                encrypted = True
        elif ln.startswith("#"):
            continue
        else:
            segs.append(Seg(urljoin(base_url, ln), pending_range, cur_dur))
            if pending_range:
                prev_end = pending_range[1] + 1
            pending_range = None
            duration += cur_dur
            cur_dur = 0.0

    return segs, init_seg, duration, encrypted


# --------------------------------------------------------------------------- #
# DASH
# --------------------------------------------------------------------------- #


def local(tag: str) -> str:
    return tag.split("}")[-1]


def iso8601_duration(s: str) -> float:
    if not s:
        return 0.0
    m = re.match(
        r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", s
    )
    if not m:
        return 0.0
    y, mo, d, h, mi, se = m.groups()
    return (
        float(y or 0) * 31536000 + float(mo or 0) * 2592000 + float(d or 0) * 86400
        + float(h or 0) * 3600 + float(mi or 0) * 60 + float(se or 0)
    )


def dash_pick_audio(root):
    best, best_bw = None, -1
    for el in root.iter():
        if local(el.tag) != "AdaptationSet":
            continue
        ct = (el.get("contentType") or "").lower()
        mime = (el.get("mimeType") or "").lower()
        if ct == "audio" or mime.startswith("audio/"):
            for rep in el:
                if local(rep.tag) == "Representation":
                    bw = int(rep.get("bandwidth") or 0)
                    if bw > best_bw:
                        best, best_bw = (el, rep), bw
                    break
            else:
                best, best_bw = (el, None), 0
    return best


def dash_segments(root, adapt, rep) -> tuple[list[Seg], Optional[Seg], float]:
    segs: list[Seg] = []
    base = ""
    for c in rep:
        if local(c.tag) == "BaseURL":
            base = (c.text or "").strip()
    rep_id = rep.get("id") or rep.get("bandwidth") or "0"

    tpl = next((c for c in rep if local(c.tag) == "SegmentTemplate"), None) or next(
        (c for c in adapt if local(c.tag) == "SegmentTemplate"), None
    )
    lst = next((c for c in rep if local(c.tag) == "SegmentList"), None) or next(
        (c for c in adapt if local(c.tag) == "SegmentList"), None
    )

    if tpl is None and lst is None:
        return [Seg(base)], None, 0.0

    if lst is not None:
        init = None
        for c in lst:
            if local(c.tag) == "Initialization" and c.get("sourceURL"):
                init = Seg(c.get("sourceURL"))
            elif local(c.tag) == "SegmentURL" and c.get("media"):
                segs.append(Seg(c.get("media")))
        return segs, init, 0.0

    media_tpl = tpl.get("media") or ""
    init_tpl = tpl.get("initialization") or ""
    start = int(tpl.get("startNumber") or 1)
    timescale = int(tpl.get("timescale") or 1)
    dur = int(tpl.get("duration") or 0)

    def fill(s: str, number: int, t: int) -> str:
        s = s.replace("$RepresentationID$", rep_id)
        s = re.sub(r"\$Bandwidth\$", rep.get("bandwidth", ""), s)
        s = re.sub(r"\$Number%0(\d+)d\$", lambda m: str(number).zfill(int(m.group(1))), s)
        s = s.replace("$Number$", str(number)).replace("$Time$", str(t))
        return s

    init = Seg(fill(init_tpl, start, 0)) if init_tpl else None

    timeline = next((c for c in tpl if local(c.tag) == "SegmentTimeline"), None)
    total = 0.0
    if timeline is not None:
        t, number = 0, start
        for s in timeline:
            if local(s.tag) != "S":
                continue
            t = int(s.get("t", t))
            d = int(s.get("d", 0))
            r = int(s.get("r", 0))
            for _ in range(r + 1):
                segs.append(Seg(fill(media_tpl, number, t), None, d / timescale))
                number += 1
                t += d
                total += d / timescale
    else:
        period_dur = 0.0
        for el in root.iter():
            if local(el.tag) == "Period" and el.get("duration"):
                period_dur = iso8601_duration(el.get("duration"))
                break
        if not period_dur:
            period_dur = iso8601_duration(root.get("mediaPresentationDuration", ""))
        if dur and period_dur:
            n = int(period_dur * timescale / dur) + 1
            for i in range(n):
                segs.append(Seg(fill(media_tpl, start + i, i * dur), None, dur / timescale))
            total = period_dur
    return segs, init, total


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


def download(session, segs: list[Seg], workers: int, limit_bytes: int, timeout: float):
    results: list[Optional[bytes]] = [None] * len(segs)
    total = 0
    aborted = False

    def fetch(seg: Seg) -> bytes:
        headers = {}
        if seg.byte_range:
            headers["Range"] = f"bytes={seg.byte_range[0]}-{seg.byte_range[1]}"
        r = session.get(seg.url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.content

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for start in range(0, len(segs), workers):
            if aborted:
                break
            batch = list(enumerate(segs[start : start + workers], start=start))
            futs = [(ex.submit(fetch, seg), i) for i, seg in batch]
            for fut, i in futs:
                try:
                    data = fut.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] 分片 {i} 失败: {exc}", file=sys.stderr)
                    continue
                results[i] = data
                total += len(data)
            if limit_bytes and total >= limit_bytes:
                aborted = True

    chunks = [c for c in results if c]
    return chunks, sum(len(c) for c in chunks), aborted


def try_decode(path: str) -> str:
    try:
        import av  # type: ignore
    except Exception:
        return "未安装 av（pip install av 可启用）"
    try:
        with av.open(path) as c:
            if not c.streams.audio:
                return "无音频流"
            st = c.streams.audio[0]
            dur = float(c.duration / av.time_base) if c.duration else 0.0
            return (
                f"codec={st.codec_context.name} rate={st.codec_context.sample_rate} "
                f"ch={st.codec_context.channels} dur={dur:.1f}s"
            )
    except Exception as e:  # noqa: BLE001
        return f"解码失败: {e}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def probe(args) -> Report:
    session = make_session(args)
    rep = Report(source=args.url)
    parsed = urlparse(args.url)
    path = parsed.path.lower()
    os.makedirs(args.out, exist_ok=True)
    limit_bytes = args.limit_mb * 1024 * 1024

    t0 = time.perf_counter()
    chunks: list[bytes] = []
    suffix = ".bin"

    if path.endswith(".m3u8") or "m3u8" in args.url:
        rep.kind = "hls"
        text, final_url = get_text(session, args.url, args.timeout)
        if hls_is_master(text):
            media_url, desc = hls_pick(text, final_url)
            rep.picked = desc
            text, final_url = get_text(session, media_url, args.timeout)
        else:
            rep.picked = "media playlist"
        segs, init_seg, duration, encrypted = hls_parse_media(text, final_url)
        rep.encrypted = encrypted
        if not segs:
            rep.notes.append("未解析到分片")
            rep.wall_s = time.perf_counter() - t0
            return rep
        allsegs = ([init_seg] if init_seg else []) + segs
        chunks, total, aborted = download(session, allsegs, args.workers, limit_bytes, args.timeout)
        rep.segments, rep.media_duration_s, rep.bytes = len(segs), duration, total
        suffix = ".m4s" if init_seg else ".ts"
        if any(s.byte_range for s in segs):
            rep.notes.append("使用了 EXT-X-BYTERANGE（单文件切片）")
        if aborted:
            rep.notes.append(f"达到 {args.limit_mb} MB 上限，提前停止")

    elif path.endswith(".mpd") or "mpd" in args.url:
        rep.kind = "dash"
        text, final_url = get_text(session, args.url, args.timeout)
        root = ET.fromstring(text)
        picked = dash_pick_audio(root)
        if not picked:
            rep.notes.append("MPD 中未找到 audio AdaptationSet")
            rep.wall_s = time.perf_counter() - t0
            return rep
        adapt, rep_el = picked
        if rep_el is None:
            rep_el = next(c for c in adapt if local(c.tag) == "Representation")
        segs, init_seg, duration = dash_segments(root, adapt, rep_el)
        rep.picked = f'audio lang={adapt.get("lang", "?")} bw={rep_el.get("bandwidth", "?")}'
        if not segs:
            rep.notes.append("未解析出分片")
            rep.wall_s = time.perf_counter() - t0
            return rep
        allsegs = ([init_seg] if init_seg else []) + segs
        chunks, total, aborted = download(session, allsegs, args.workers, limit_bytes, args.timeout)
        rep.segments, rep.media_duration_s, rep.bytes = len(segs), duration, total
        suffix = ".mp4"
        if aborted:
            rep.notes.append(f"达到 {args.limit_mb} MB 上限，提前停止")

    else:
        rep.kind = "direct"
        rep.picked = "直链（流式读取）"
        r = session.get(args.url, stream=True, timeout=args.timeout)
        r.raise_for_status()
        rep.notes.append(f"content-type={r.headers.get('content-type', '?')}")
        cl = r.headers.get("content-length")
        if cl:
            rep.notes.append(f"content-length={int(cl)/1e6:.1f} MB")
        buf = bytearray()
        for chunk in r.iter_content(256 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) >= limit_bytes:
                rep.notes.append(f"达到 {args.limit_mb} MB 上限，提前停止")
                break
        chunks = [bytes(buf)]
        rep.bytes, rep.segments = len(buf), 1
        ext = os.path.splitext(path)[1]
        suffix = ext if ext and len(ext) <= 6 else ".bin"

    rep.wall_s = time.perf_counter() - t0
    out_path = os.path.join(args.out, safe_name(args.url, suffix))
    with open(out_path, "wb") as f:
        for c in chunks:
            f.write(c)
    rep.out_path = out_path
    rep.decode = try_decode(out_path)
    return rep


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="测量网页视频音轨的获取速度")
    p.add_argument("--url", required=True)
    p.add_argument("--referer", default="")
    p.add_argument("--cookie", default="")
    p.add_argument("--user-agent", default="")
    p.add_argument("--out", default="data")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit-mb", type=int, default=20)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)

    try:
        rep = probe(args)
    except requests.HTTPError as e:
        print(f"[HTTP 错误] {e}\n  403/401 通常需要 --referer 或 --cookie", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(rep.summary())
    if args.json:
        d = asdict(rep)
        d.update({"mbps": rep.mbps, "speed_x": rep.speed_x, "kbps": rep.kbps})
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"结果写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
