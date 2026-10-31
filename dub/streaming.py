"""分块流水线（streaming）：长音频切 ~60s 段，段 0 最先出声，后台顺序推进。

- 切点：目标整分钟附近找最安静的 0.4s 窗口（能量法，不依赖 VAD）
- 说话人：全局注册表——每句算 CAM++ 嵌入，与已有质心 cos≥0.55 归并，
  新人现场建档（F0 画像）。跨段说话人编号稳定，这是分段的正确性关键。
- 段内混音用全轨统一增益（避免逐段峰值归一造成的段间响度跳变）
- 任务目录 = 内容哈希+语言+后端，state.json 落盘：中断续跑、完成秒回
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import numpy as np

from . import audio, diarize, stages
from .profile import classify, f0_stats, spectral_centroid_hz

CHUNK_SEC = 60.0
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
               duck_db: float = -8.0, max_stretch: float = 1.25) -> None:
    """段内混音：与 stages.assemble 同逻辑，但用全局统一增益替代逐段峰值归一。"""
    bed, sr = audio.load(bed_path, sr=stages.SR_MIX)
    if bed.shape[1] == 1:
        bed = np.repeat(bed, 2, axis=1)
    dub = np.zeros_like(bed)
    for s in segs:
        if not s.tts_path or not os.path.exists(s.tts_path):
            continue
        clip, _ = audio.load(s.tts_path, sr=stages.SR_MIX, mono=False)
        if clip.shape[1] == 1:
            clip = np.repeat(clip, 2, axis=1)
        if s.duration > 0 and len(clip) / stages.SR_MIX > s.duration * 1.05:
            rate = min(max_stretch, (len(clip) / stages.SR_MIX) / s.duration)
            clip = audio.time_stretch(clip, stages.SR_MIX, rate)
        start = int(s.start * stages.SR_MIX)
        audio.place_fade(dub, clip, start)
    spans = [(int(s.start * sr), int(s.end * sr)) for s in segs]
    env = audio.db_envelope(len(bed), spans, sr, duck_db)
    mix = bed * env[:, None] + dub
    mix *= gain
    audio.save(out_wav, np.clip(mix, -1.0, 1.0), sr)


def run_job(job: dict, src: str, lang: str, source_lang: str, tts_backend: str,
            workdir: str, api_base: str = "", api_key: str = "", llm_model: str = "") -> None:
    """线程主体。job dict 由 serve 持有，状态同时落盘以便续跑/秒回。"""
    t0 = time.perf_counter()
    base = os.path.splitext(os.path.basename(src))[0]
    job_dir = os.path.join(workdir, "work", "stream", f"{base}_{lang}_{tts_backend}")
    os.makedirs(job_dir, exist_ok=True)
    job["dir"] = job_dir
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

        segs = stages._asr_transformers(vocals, source_lang or None, "auto")
        segs = stages._filter_hallucinations(segs)

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

        stages.translate(segs, backend="llm", target=lang, chars_per_sec=cps,
                         api_base=api_base, api_key=api_key, model=llm_model)
        stages.tts(segs, os.path.join(cdir, "tts"), backend=tts_backend,
                   lang=lang, profiles=reg.profiles)

        _mix_chunk(bed, segs, os.path.join(cdir, "chunk.wav"), gain)
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
        for ch in state["chunks"]:
            if ch["status"] == "done":
                continue
            if round_no == 2 and ch["i"] not in errors:
                continue
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
