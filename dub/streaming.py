"""分块流水线（streaming）：长音频切 ~60s 段，段 0 最先出声，后台顺序推进。

- 切点：目标整分钟附近找最安静的 0.4s 窗口（能量法，不依赖 VAD）
- 说话人：全局注册表——每句算 CAM++ 嵌入，与已有质心 cos≥0.55 归并，
  新人现场建档（F0 画像）。跨段说话人编号稳定，这是分段的正确性关键。
- 段内混音用全轨统一增益（避免逐段峰值归一造成的段间响度跳变）
- 任务目录 = 内容哈希+语言+后端+流水线版本，state.json 落盘：中断续跑、完成秒回
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

import numpy as np

from . import audio, diarize, stages
from .profile import classify, f0_stats, spectral_centroid_hz

CHUNK_SEC = 60.0
# 配音覆盖区外的人声干声压低量（dB）：覆盖区内压低让位配音，
# 覆盖区外原样播出——识别漏掉的句子不再变成静音洞
VOC_KEEP_DB = -18.0
# 对齐/混音逻辑升级时 +1（p3 → p4 …）：任务目录随之换新，
# "重新配音"才会真正重跑而不是命中旧缓存；旧目录原样保留作退路
PIPELINE_VER = "p4"
# 目标语言 → 每秒字数（翻译预算）
CPS = {"zh": 4.5, "en": 12.0, "ja": 7.0, "ko": 7.0}


def find_cuts(mix_path: str) -> list[float]:
    """返回全局切点秒数（含 0 与总长）。在整分钟 ±5s 内找最安静处下刀。"""
    data, sr = audio.load(mix_path, sr=8000, mono=True)
    data = data[:, 0]
    dur = len(data) / sr
    win = int(0.4 * sr)
    cuts = [0.0]
    t = CHUNK_SEC
    while t < dur - 15:
        center = int(t * sr)
        radius = int(5 * sr)
        lo, hi = max(0, center - radius), min(len(data) - win, center + radius)
        if hi <= lo:
            cuts.append(t)
            t += CHUNK_SEC
            continue
        e = np.convolve(data[lo:hi] ** 2, np.ones(win) / win, "valid")
        cuts.append((lo + int(np.argmin(e))) / sr)
        t += CHUNK_SEC
    cuts.append(dur)
    return cuts


class SpeakerRegistry:
    def __init__(self, centroids: list | None = None, profiles: dict | None = None):
        self.centroids = [np.array(c, dtype=np.float32) for c in (centroids or [])]
        self.profiles: dict[int, dict] = {int(k): v for k, v in (profiles or {}).items()}

    def assign(self, emb16: list[float]) -> int:
        e = np.array(emb16, dtype=np.float32)
        if not self.centroids:
            self.centroids.append(e)
            return 0
        sims = [float(c @ e) for c in self.centroids]
        k = int(np.argmax(sims))
        if sims[k] >= 0.55:
            self.centroids[k] = self.centroids[k] * 0.8 + e * 0.2  # 质心缓动
            self.centroids[k] /= np.linalg.norm(self.centroids[k])
            return k
        self.centroids.append(e)
        return len(self.centroids) - 1

    def ensure_profile(self, spk: int, chunks: list[np.ndarray]) -> None:
        """新说话人现场建档：用其在本段内的音频算 F0 画像。"""
        if spk in self.profiles or not chunks:
            return
        wav = np.concatenate(chunks)
        f0, f0std = f0_stats(wav)
        cen = spectral_centroid_hz(wav)
        g, a = classify(f0, f0std, cen)
        self.profiles[spk] = {"f0": round(f0, 1), "f0std": round(f0std, 1),
                              "centroid": round(cen), "gender": g, "age": a}

    def dump(self) -> dict:
        return {
            "centroids": [c.tolist() for c in self.centroids],
            "profiles": {str(k): v for k, v in self.profiles.items()},
        }

    def consolidate(self) -> dict[int, int]:
        """画像归并：同 gender 且 F0 相对差 ≤25% 的说话人链式归并。
        BGM 残留/情绪起伏会把同一人拆成多个 ID（实测 25min 片拆出 23 个），
        表现为角色中途换声。质心按成员均值重组，后续 assign 直接落到归并后的类；
        返回 旧id→新id 映射，供本段已分配的句子改写。"""
        ids = sorted(set(self.profiles) | set(range(len(self.centroids))))
        parent = {i: i for i in ids}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        prof_ids = [i for i in ids if i in self.profiles]
        for a in range(len(prof_ids)):
            for b in range(a + 1, len(prof_ids)):
                pa, pb = self.profiles[prof_ids[a]], self.profiles[prof_ids[b]]
                if pa.get("gender") == "x" or pa.get("gender") != pb.get("gender"):
                    continue
                fa, fb = pa.get("f0") or 0.0, pb.get("f0") or 0.0
                if fa <= 0 or fb <= 0 or abs(fa - fb) > 0.25 * min(fa, fb):
                    continue
                ra, rb = find(prof_ids[a]), find(prof_ids[b])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

        groups: dict[int, list[int]] = {}
        for i in ids:
            groups.setdefault(find(i), []).append(i)
        ordered = sorted(groups.values(), key=lambda ms: min(ms))
        mapping = {m: k for k, members in enumerate(ordered) for m in members}
        if all(mapping[i] == i for i in ids):
            return mapping

        cents, profs = [], {}
        for members in ordered:
            cs = [self.centroids[m] for m in members if m < len(self.centroids)]
            c = np.mean(cs, axis=0) if cs else np.zeros(192, dtype=np.float32)
            n = float(np.linalg.norm(c))
            cents.append((c / n if n > 0 else c).astype(np.float32))
            ms = [self.profiles[m] for m in members if m in self.profiles]
            if ms:
                f0 = float(np.mean([p.get("f0") or 0.0 for p in ms]))
                f0std = float(np.mean([p.get("f0std") or 0.0 for p in ms]))
                cen = float(np.mean([p.get("centroid") or 0.0 for p in ms]))
                g, age = classify(f0, f0std, cen) if f0 > 0 else ("x", "young")
                profs[len(cents) - 1] = {"f0": round(f0, 1), "f0std": round(f0std, 1),
                                         "centroid": round(cen), "gender": g, "age": age}
        self.centroids = cents
        self.profiles = profs
        return mapping


def _f0(wav: np.ndarray) -> tuple[float, float]:
    from .profile import f0_stats

    return f0_stats(wav)


def _centroid(wav: np.ndarray) -> float:
    from .profile import spectral_centroid_hz

    return spectral_centroid_hz(wav)


def _slice_wav(src: str, dst: str, a: float, b: float, sr: int = 44100) -> None:
    import soundfile as sf

    native = sf.info(src).samplerate
    data, _ = sf.read(src, start=int(a * native), stop=int(b * native),
                      always_2d=True, dtype="float32")
    if native != sr:
        data = audio.resample(data, native, sr)
    audio.save(dst, data, sr)


def _to_mp3(wav: str, mp3: str) -> None:
    import imageio_ffmpeg

    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
         "-i", wav, "-codec:a", "libmp3lame", "-qscale:a", "2", mp3],
        check=True,
    )


def _mix_chunk(bed_path: str, segs: list, out_wav: str, gain: float,
               duck_db: float = -8.0, max_stretch: float = 1.35,
               voc_path: str = "") -> None:
    """段内混音：每句起点对齐原句（口型起手），速率贴合到"下一句起点"
    ——台词节奏跟表演走，不与下一句串位。全局统一增益防响度跳变。
    voc_path：人声干声。配音覆盖区外原样回放——VAD/ASR 漏掉的句子
    （以及纯歌声）播出原声而不是留下静音洞。"""
    bed, sr = audio.load(bed_path, sr=stages.SR_MIX)
    if bed.shape[1] == 1:
        bed = np.repeat(bed, 2, axis=1)
    dub = np.zeros_like(bed)
    for idx, s in enumerate(segs):
        if not s.tts_path or not os.path.exists(s.tts_path):
            continue
        clip, _ = audio.load(s.tts_path, sr=stages.SR_MIX, mono=False)
        if clip.shape[1] == 1:
            clip = np.repeat(clip, 2, axis=1)
        # 窗口预算：本句起点 → 下一句起点（句间静音也归本句支配）
        if idx + 1 < len(segs):
            budget = max(s.duration, segs[idx + 1].start - s.start)
        else:
            budget = max(s.duration, (len(bed) / sr) - s.start)
        dur = len(clip) / stages.SR_MIX
        if budget > 0 and dur > budget:
            cap = 1.45 if budget < 4 else max_stretch  # 短句可以压得更快，听感无碍
            rate = min(cap, dur / budget)
            if rate > 1.01:
                clip = audio.time_stretch(clip, stages.SR_MIX, rate)
        start = int(s.start * stages.SR_MIX)
        audio.place_fade(dub, clip, start)
    spans = [(int(s.start * sr), int(s.end * sr)) for s in segs]
    env = audio.db_envelope(len(bed), spans, sr, duck_db)
    mix = bed * env[:, None] + dub
    if voc_path and os.path.exists(voc_path):
        voc, _ = audio.load(voc_path, sr=sr)
        if voc.shape[1] == 1:
            voc = np.repeat(voc, 2, axis=1)
        n = min(len(voc), len(bed))
        env_voc = audio.db_envelope(n, spans, sr, VOC_KEEP_DB)
        mix[:n] += voc[:n] * env_voc[:, None]
    mix *= gain
    audio.save(out_wav, np.clip(mix, -1.0, 1.0), sr)


def _pick_next(todo: list[dict], pos: float | None) -> dict:
    """挑下一个要配的段。无位置信息按播放顺序；有位置则**离播放头最近的段优先**
    （播放头落在段内距离记 0；同距偏向前方——用户更可能继续往后看）。
    这解决"用户已看到 6 分钟，流水线还在配 2 分钟"的错位。"""
    if pos is None:
        return todo[0]

    def dist(c: dict) -> float:
        d = min(max(pos, c["start"]), c["end"]) - pos
        return abs(d) + (0.0 if c["end"] > pos else 0.5)

    return min(todo, key=dist)


def _write_meta(job_dir: str, meta: dict) -> None:
    """任务元信息落盘（管理页显示真名用）：页面标题尽力抓一次，失败退 referer。"""
    p = os.path.join(job_dir, "meta.json")
    meta = dict(meta)
    meta.setdefault("created", int(time.time()))
    if os.path.exists(p):  # 续跑时保留已抓到的标题，不重复抓页面
        try:
            old = json.load(open(p, encoding="utf-8"))
            old.update({k: v for k, v in meta.items() if v})
            meta = old
        except Exception:
            pass
    if meta.get("page") and not meta.get("title"):
        try:
            import requests

            # trust_env 是 Session 属性（requests.get 不认），B 站对简陋 UA 会拒
            sess = requests.Session()
            sess.trust_env = False
            sess.headers.update({"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")})
            r = sess.get(meta["page"], timeout=(3, 5))
            r.encoding = r.apparent_encoding or "utf-8"
            m = re.search(r"<title>([^<]{1,200})</title>", r.text[:65536], re.I)
            if m:
                t = m.group(1).strip()
                for suf in ("_哔哩哔哩_bilibili", "_bilibili", " - YouTube", "- YouTube"):
                    if t.endswith(suf):
                        t = t[: -len(suf)]
                meta["title"] = t.strip()[:120] or meta["page"]
        except Exception:
            pass
    try:
        json.dump(meta, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def run_job(job: dict, src: str, lang: str, source_lang: str, tts_backend: str,
            workdir: str, api_base: str = "", api_key: str = "", llm_model: str = "",
            mimo_base: str = "", mimo_key: str = "") -> None:
    """线程主体。job dict 由 serve 持有，状态同时落盘以便续跑/秒回。"""
    t0 = time.perf_counter()
    base = os.path.splitext(os.path.basename(src))[0]
    job_dir = os.path.join(workdir, "work", "stream",
                           f"{base}_{lang}_{tts_backend}_{PIPELINE_VER}")
    os.makedirs(job_dir, exist_ok=True)
    job["dir"] = job_dir
    _write_meta(job_dir, job.get("meta") or {})
    state_path = os.path.join(job_dir, "state.json")

    if os.path.exists(state_path):
        state = json.load(open(state_path, encoding="utf-8"))
        # 上次中断在半路的段归位为待跑
        for ch in state.get("chunks", []):
            if ch["status"] == "running":
                ch["status"] = "pending"
    else:
        state = {"chunks": [], "registry": None, "lang": lang}

    # 1) 整轨抽取（内容哈希文件 → mix.wav，只做一次）
    mix = os.path.join(job_dir, "mix.wav")
    if not os.path.exists(mix):
        job["stage"] = "extract"
        stages.extract_audio(src, mix)

    # 2) 切点
    cuts = find_cuts(mix)
    n_chunks = len(cuts) - 1
    if not state["chunks"]:
        state["chunks"] = [{"i": i, "start": cuts[i], "end": cuts[i + 1], "status": "pending"}
                           for i in range(n_chunks)]
    job["chunks"] = state["chunks"]
    job["duration"] = cuts[-1]

    # 全轨峰值 → 统一增益（目标 -1 dBFS）
    peak = 0.0
    import soundfile as sf

    for blk in sf.blocks(mix, blocksize=1 << 22, dtype="float32"):
        peak = max(peak, float(np.max(np.abs(blk))))
    gain = (10 ** (-1.0 / 20.0)) / max(peak, 1e-9)

    reg = SpeakerRegistry((state.get("registry") or {}).get("centroids"),
                          (state.get("registry") or {}).get("profiles"))
    cps = CPS.get(lang, 4.5)

    # 3) 逐段推进（两遍：单段失败不杀任务，先跑完其余段再回头重试失败段）
    def process_chunk(ch: dict) -> None:
        i, a, b = ch["i"], ch["start"], ch["end"]
        ch["status"] = "running"
        job["stage"] = f"chunk {i + 1}/{n_chunks}"
        json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
        cdir = os.path.join(job_dir, f"c{i:03d}")
        os.makedirs(cdir, exist_ok=True)

        cmix = os.path.join(cdir, "mix.wav")
        _slice_wav(mix, cmix, a, b)

        vocals, bed = stages.separate(cmix, cdir)

        segs = stages._asr_transformers(vocals, source_lang or None, "auto", merge_gap=0.15)
        segs = stages._filter_hallucinations(segs)
        segs = stages._split_long(segs)  # 连续解说的句子级切分，配音节奏对齐的关键

        v16, sr16 = audio.load(vocals, sr=16000, mono=True)
        last_emb = None
        by_spk_audio: dict[int, list] = {}
        for s in segs:
            seg = v16[int(s.start * sr16):int(s.end * sr16), 0]
            if len(seg) >= sr16:
                last_emb = diarize.embed(seg)
                s.speaker = reg.assign(last_emb)
            else:
                s.speaker = reg.assign(last_emb) if last_emb is not None else 0
            by_spk_audio.setdefault(s.speaker, []).append(seg)
        for spk, wavs in by_spk_audio.items():
            reg.ensure_profile(spk, wavs)

        # 碎片说话人归并（同 gender+F0 相近链式合并），本段句子同步改写
        remap = reg.consolidate()
        if any(k != v for k, v in remap.items()):
            for s in segs:
                s.speaker = remap.get(s.speaker, s.speaker)
            print(f"    [spk] 画像归并: {len(remap)} 个 ID -> {len(set(remap.values()))} 类",
                  flush=True)

        stages.translate(segs, backend="llm", target=lang, chars_per_sec=cps,
                         api_base=api_base, api_key=api_key, model=llm_model)
        stages.tts(segs, os.path.join(cdir, "tts"), backend=tts_backend,
                   lang=lang, profiles=reg.profiles,
                   mimo_cfg={"base": mimo_base, "key": mimo_key})

        _mix_chunk(bed, segs, os.path.join(cdir, "chunk.wav"), gain, voc_path=vocals)
        mp3 = os.path.join(cdir, "chunk.mp3")
        _to_mp3(os.path.join(cdir, "chunk.wav"), mp3)
        ch["status"] = "done"
        ch["mp3"] = mp3
        job["chunks_done"] = sum(1 for c in state["chunks"] if c["status"] == "done")
        ch["segments"] = [
            {"start": a + s.start, "end": a + s.end,
             "text": s.translated or s.text, "speaker": s.speaker}
            for s in segs
        ]

    errors: dict[int, str] = {}
    for round_no in (1, 2):  # 第二遍只重试第一遍失败的段
        if round_no == 1:
            todo = [c for c in state["chunks"] if c["status"] != "done"]
        else:
            todo = [c for c in state["chunks"] if c["i"] in errors]
        while todo:
            # 每次挑段都重读播放位置：用户中途拖动，调度跟着改主意
            ch = _pick_next(todo, job.get("pos"))
            todo = [c for c in todo if c is not ch]
            try:
                process_chunk(ch)
                errors.pop(ch["i"], None)
            except Exception as exc:
                ch["status"] = "error"
                errors[ch["i"]] = f"{type(exc).__name__}: {exc}"
                job["error"] = f"段 {ch['i'] + 1}: {errors[ch['i']]}"
            json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
        if not errors:
            break
        if round_no == 1 and errors:
            print(f"    [stream] 第一遍失败 {len(errors)} 段，重试中…", flush=True)

    # 4) 收尾：全部成功 → 拼整轨；有失败段 → partial（已完成段照常可听，可重试）
    state["registry"] = reg.dump()
    json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
    if errors:
        job["status"] = "partial"
        job["stage"] = "partial"
        job["error"] = "；".join(f"段 {i + 1}: {m}" for i, m in sorted(errors.items()))
        job["total_s"] = round(time.perf_counter() - t0, 1)
        job["segments_total"] = sum(len(c.get("segments") or []) for c in state["chunks"])
        return

    job["stage"] = "final"
    final_wav = os.path.join(job_dir, "final.wav")
    if not os.path.exists(final_wav):
        import soundfile as sf

        parts = []
        for ch in state["chunks"]:
            w = os.path.join(job_dir, f"c{ch['i']:03d}", "chunk.wav")
            d, _ = sf.read(w, dtype="float32", always_2d=True)
            parts.append(d)
        audio.save(final_wav, np.concatenate(parts), stages.SR_MIX)
    final_mp3 = os.path.join(job_dir, "final.mp3")
    if not os.path.exists(final_mp3):
        _to_mp3(final_wav, final_mp3)

    job["status"] = "done"
    job["stage"] = "done"
    job["total_s"] = round(time.perf_counter() - t0, 1)
    job["segments_total"] = sum(len(c.get("segments") or []) for c in state["chunks"])
    json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
