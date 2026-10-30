"""各阶段实现。重型依赖全部延迟导入，缺了也能退回 stub。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import numpy as np

from . import audio

SR_MIX = 44100
SR_TTS = 24000


def resolve_device(device: str) -> str:
    if device and device != "auto":
        return device
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


@dataclass
class Segment:
    start: float
    end: float
    text: str = ""
    translated: str = ""
    tts_path: str = ""
    speaker: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "translated": self.translated,
            "tts_path": self.tts_path,
            "speaker": self.speaker,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(d["start"], d["end"], d.get("text", ""), d.get("translated", ""),
                   d.get("tts_path", ""), d.get("speaker", 0))


@dataclass
class Stats:
    segments: int = 0
    overflow: int = 0
    translated_chars: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1. 抽取音轨
# --------------------------------------------------------------------------- #


def extract_audio(src: str, dst: str, sr: int = SR_MIX, channels: int = 2) -> float:
    """任意容器 → wav。返回时长（秒）。"""
    import av

    container = av.open(src)
    if not container.streams.audio:
        raise RuntimeError(f"{src} 里没有音频流")
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(
        format="fltp", layout="stereo" if channels == 2 else "mono", rate=sr
    )
    chunks = []
    try:
        for frame in container.decode(stream):
            for f in resampler.resample(frame):
                chunks.append(f.to_ndarray())
    except Exception as exc:  # 截断文件
        print(f"[warn] 解码中断（可能是截断）：{exc}", file=sys.stderr)
    for f in resampler.resample(None):
        chunks.append(f.to_ndarray())
    container.close()

    if not chunks:
        raise RuntimeError("没有解出任何音频帧")
    # fltp: (ch, n) → 拼成 (n, ch)
    data = np.concatenate(chunks, axis=1).T.astype(np.float32)
    audio.save(dst, data, sr)
    return len(data) / sr


# --------------------------------------------------------------------------- #
# 2. 人声分离
# --------------------------------------------------------------------------- #


def separate(mix_wav: str, out_dir: str, device: str = "cuda", model_name: str = "htdemucs") -> tuple[str, str]:
    """返回 (vocals_path, bed_path)。失败则抛异常，由调用方决定是否 duck 兜底。"""
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    # 不用 torchaudio.load：2.10 起它依赖 torchcodec，而我们手里本来就是 wav
    data, sr = audio.load(mix_wav, sr=SR_MIX)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    wav = torch.from_numpy(data.T.copy())  # (ch, n)

    model = get_model(model_name)
    model.eval()
    device = resolve_device(device)
    model.to(device)

    ref = wav.mean(0)
    norm = (wav - ref.mean()) / (ref.std() + 1e-8)
    with torch.no_grad():
        sources = apply_model(model, norm[None], device=device, progress=False)[0]
    sources = sources * ref.std() + ref.mean()

    names = list(model.sources)
    if "vocals" not in names:
        raise RuntimeError(f"模型 {model_name} 没有 vocals 轨：{names}")
    vi = names.index("vocals")
    vocals = sources[vi].cpu().numpy().T
    bed = sources[[i for i in range(len(names)) if i != vi]].sum(0).cpu().numpy().T

    vpath = os.path.join(out_dir, "vocals.wav")
    bpath = os.path.join(out_dir, "bed.wav")
    audio.save(vpath, vocals, SR_MIX)
    audio.save(bpath, bed, SR_MIX)
    return vpath, bpath


# --------------------------------------------------------------------------- #
# 3. 识别
# --------------------------------------------------------------------------- #


def asr(path: str, backend: str = "auto", model: str = "large-v3-turbo",
        language: str | None = None, device: str = "auto") -> list[Segment]:
    if backend == "stub":
        data, sr = audio.load(path, sr=16000, mono=True)
        dur = len(data) / 16000
        out, t = [], 1.0
        while t < dur - 1.0:
            end = min(t + 8.0, dur)
            out.append(Segment(t, end, "[stub asr]"))
            t = end + 0.5
        return out
    if backend in ("auto", "faster"):
        try:
            segs = _filter_hallucinations(_asr_faster(path, model, language, device))
            return _diarize_safe(path, segs)
        except Exception as exc:
            if backend == "faster":
                raise
            print(f"[warn] faster-whisper 不可用（{exc}），改用 transformers", file=sys.stderr)
    if backend in ("auto", "transformers"):
        segs = _filter_hallucinations(_asr_transformers(path, language, device))
        return _diarize_safe(path, segs)
    raise ValueError(f"未知 ASR backend: {backend}")


def _diarize_safe(path: str, segs: list[Segment]) -> list[Segment]:
    """diarize 失败不毁管线：全标 0 继续走（所有台词同一音色）。"""
    try:
        from .diarize import diarize

        return diarize(path, segs)
    except Exception as exc:
        print(f"[warn] diarize 失败（{type(exc).__name__}: {exc}），全部标为说话人 0", file=sys.stderr)
        for s in segs:
            s.speaker = 0
        return segs


def _asr_faster(path: str, model: str, language: str | None, device: str) -> list[Segment]:
    from faster_whisper import WhisperModel

    dev = "cuda" if resolve_device(device) == "cuda" else "cpu"
    try:
        m = WhisperModel(model, device=dev, compute_type="float16" if dev == "cuda" else "int8")
        segs, _ = m.transcribe(path, language=language, vad_filter=True, beam_size=5)
        return [Segment(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]
    except Exception:
        if dev == "cpu":
            raise
        m = WhisperModel(model, device="cpu", compute_type="int8")
        segs, _ = m.transcribe(path, language=language, vad_filter=True, beam_size=5)
        return [Segment(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]


def _is_loopy(text: str) -> bool:
    """whisper 幻觉循环检测：同一 bigram 或同一词反复出现。

    幻觉多发生在配乐吟唱/无解说段（demucs 会把吟唱分进人声轨，能量不低，
    只能靠文本信号）。真台词偶有叠词，但 top bigram 占比到不了阈值。
    """
    import re
    from collections import Counter

    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.lower()).strip()
    if len(t) < 4:
        return False
    grams = Counter(t[i : i + 2] for i in range(len(t) - 1))
    _, top_c = grams.most_common(1)[0]
    if top_c >= 3 and top_c / len(t) > 0.2:
        return True
    words = t.split()
    if len(words) <= 10:
        wc = Counter(w for w in words if len(w) >= 2)
        if wc and wc.most_common(1)[0][1] >= 3:
            return True
    return False


def _grams(text: str) -> set[str]:
    import re

    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _filter_hallucinations(segs: list[Segment]) -> list[Segment]:
    """幻觉过滤。R1：自身循环（同 bigram/词反复）。
    R2：相邻段 bigram Jaccard ≥ 0.55 连成串、串长 ≥3 → 幻觉串整串丢弃
    （幻觉串的特征是段段雷同，不是共享某个实词——中文真解说会高频复现
    "植物"这类主题词，按共享词判会误杀）。"""
    grams = [_grams(s.text) for s in segs]
    kill = set()

    def near(a: set, b: set) -> float:
        """包含率：短 bigram 集合被长集合吃掉的比例。幻觉串段段同源，
        短段基本是长段的子集；真解说相邻段内容不同，交集很小。"""
        return len(a & b) / min(len(a), len(b)) if a and b else 0.0

    i = 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs) and near(grams[j], grams[j + 1]) >= 0.5:
            j += 1
        if j - i + 1 >= 3:
            kill.update(range(i, j + 1))
        i = j + 1

    out = [s for k, s in enumerate(segs) if k not in kill and not _is_loopy(s.text)]
    dropped = len(segs) - len(out)
    if dropped:
        print(f"    [asr] 丢弃幻觉段 {dropped}/{len(segs)}")
    return out


def vad_regions(data16k: np.ndarray, sr: int = 16000, pad_ms: int = 150,
                min_speech_ms: int = 300, min_silence_ms: int = 350,
                merge_gap: float = 2.0, min_region: float = 1.2) -> list[tuple[float, float]]:
    """silero VAD 切语音区。返回 [(start_s, end_s)]。模型随 pip 包内置，离线。

    merge_gap：相邻区间隙小于它就并成一个区（whisper 区内自己分段，区太碎
    窗口装不下配音）；min_region：孤立短区直接丢（拟声/背景音）。
    """
    from silero_vad import get_speech_timestamps, load_silero_vad

    model = load_silero_vad()
    ts = get_speech_timestamps(
        data16k, model, sampling_rate=sr,
        min_speech_duration_ms=min_speech_ms, min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=pad_ms,
    )
    regions = [(x["start"] / sr, x["end"] / sr) for x in ts]

    merged: list[list[float]] = []
    for a, b in regions:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = [(a, b) for a, b in merged if b - a >= min_region or len(merged) == 1]
    return out


def _asr_transformers(path: str, language: str | None, device: str) -> list[Segment]:
    import torch
    from transformers import pipeline

    model_id = "openai/whisper-large-v3-turbo"
    dev = 0 if (device in ("auto", "cuda") and torch.cuda.is_available()) else -1
    pipe = pipeline("automatic-speech-recognition", model=model_id, device=dev,
                    torch_dtype=torch.float16 if dev == 0 else torch.float32)
    data, sr = audio.load(path, sr=16000, mono=True)
    regions = vad_regions(data[:, 0])
    print(f"    [vad] 语音区 {len(regions)} 个，共 {sum(b-a for a, b in regions):.1f}s / {len(data)/sr:.0f}s")
    kwargs = {"return_timestamps": True}
    if language:
        kwargs["generate_kwargs"] = {"language": language}
    segs = []
    for a, b in regions:
        clip = data[int(a * sr):int(b * sr), 0]
        if len(clip) < int(1.2 * sr):
            continue
        out = pipe({"array": clip, "sampling_rate": sr}, **kwargs)
        for c in out.get("chunks") or []:
            ts = c.get("timestamp") or (0, 0)
            if ts[0] is None or ts[1] is None:
                continue
            txt = (c.get("text") or "").strip()
            if txt:
                segs.append(Segment(a + float(ts[0]), a + float(ts[1]), txt))

    # 间隙小于 0.35s 的相邻段合并：whisper 常把一句拆成碎窗，碎窗装不下配音
    merged: list[Segment] = []
    for s in segs:
        if merged and s.start - merged[-1].end <= 0.35:
            merged[-1].end = s.end
            merged[-1].text = merged[-1].text.rstrip("，。,. ") + " " + s.text.lstrip("，。,. ")
        else:
            merged.append(s)
    return merged


# --------------------------------------------------------------------------- #
# 4. 翻译
# --------------------------------------------------------------------------- #

LANG_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語", "ko": "한국어"}

SYSTEM_PROMPT = """你是专业的影视配音翻译。把输入的每一行字幕翻译成{lang}，要求：
1. 口语化，像配音演员在说话，不要书面语；
2. 每行译文长度不超过 max_chars 个字符（这是为了塞进原来的时间窗，超了会盖住下一句）；
3. 保留人名、专有名词的译法一致；拟声词优先保留原样；
4. 不要合并或拆分行，保持行数一致；
5. 只输出 JSON：{{"lines":[{{"i":1,"t":"译文"}}]}}"""


def translate(segments: list[Segment], backend: str = "stub", target: str = "zh",
              chars_per_sec: float = 4.5, glossary: dict[str, str] | None = None,
              api_base: str = "", api_key: str = "", model: str = "") -> Stats:
    stats = Stats(segments=len(segments))
    if not segments:
        return stats

    if backend == "stub":
        for s in segments:
            budget = max(4, int(s.duration * chars_per_sec))
            s.translated = ("[译]" + s.text)[:budget]
        stats.translated_chars = sum(len(s.translated) for s in segments)
        return stats

    api_base = (api_base or os.environ.get("DUB_LLM_BASE", "")).rstrip("/")
    api_key = api_key or os.environ.get("DUB_LLM_KEY", "")
    model = model or os.environ.get("DUB_LLM_MODEL", "")
    if not api_base or not api_key or not model:
        raise RuntimeError("缺少 LLM 配置：--api-base/--api-key/--model 或 DUB_LLM_BASE/DUB_LLM_KEY/DUB_LLM_MODEL")

    import requests

    for i in range(0, len(segments), 20):
        batch = segments[i : i + 20]
        lines = []
        for j, s in enumerate(batch, 1):
            budget = max(4, int(s.duration * chars_per_sec))
            lines.append(f'{j}. (max_chars={budget}) {s.text}')
        user = "\n".join(lines)
        payload = {
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(lang=LANG_NAMES.get(target, target))},
                {"role": "user", "content": user},
            ],
        }
        r = requests.post(f"{api_base}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}"},
                          json=payload, timeout=120)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        out = _parse_lines(text)
        for j, s in enumerate(batch, 1):
            s.translated = out.get(j, "") or s.text

    if glossary:
        for s in segments:
            for k, v in glossary.items():
                s.translated = s.translated.replace(k, v)
    stats.translated_chars = sum(len(s.translated) for s in segments)
    return stats


def _parse_lines(text: str) -> dict[int, str]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        data = json.loads(text)
        return {int(x["i"]): str(x.get("t", "")).strip() for x in data.get("lines", [])}
    except Exception:
        pass
    # 退化：按 "1. xxx" 抓
    out = {}
    for m in re.finditer(r"^\s*(\d+)[.、)]\s*(.+)$", text, re.M):
        out[int(m.group(1))] = m.group(2).strip()
    return out


# --------------------------------------------------------------------------- #
# 5. 合成
# --------------------------------------------------------------------------- #

EDGE_VOICES = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "zh-male": "zh-CN-YunxiNeural",
    "en": "en-US-AriaNeural",
    "en-male": "en-US-GuyNeural",
}

# 说话人 → 音色池（diarize 出的 speaker 序号索引）；--voice 显式给则全程单音色
EDGE_SPEAKER_VOICES = {
    "zh": ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunyangNeural"],
    "en": ["en-US-AriaNeural", "en-US-GuyNeural", "en-US-MichelleNeural", "en-US-RogerNeural"],
}

# 声学画像 → 音色（性别/年龄必须对得上，老年男不配年轻女）。
# 有画像时优先用这张表；缺的组合回退到上面的序号池。
EDGE_VOICE_BY_PROFILE = {
    "zh": {
        ("m", "child"): "zh-CN-YunxiaNeural",
        ("m", "young"): "zh-CN-YunxiNeural",
        ("m", "mature"): "zh-CN-YunyangNeural",
        ("f", "child"): "zh-CN-XiaoxiaoNeural",
        ("f", "young"): "zh-CN-XiaoxiaoNeural",
        ("f", "mature"): "zh-CN-XiaoyiNeural",
    },
    "en": {
        ("m", "child"): "en-US-GuyNeural",
        ("m", "young"): "en-US-AndrewNeural",
        ("m", "mature"): "en-US-RogerNeural",
        ("f", "child"): "en-US-AnaNeural",
        ("f", "young"): "en-US-AriaNeural",
        ("f", "mature"): "en-US-MichelleNeural",
    },
}


def tts(segments: list[Segment], out_dir: str, backend: str = "stub", voice: str = "",
        device: str = "auto", ref_wav: str = "", lang: str = "zh",
        profiles: dict | None = None) -> Stats:
    os.makedirs(out_dir, exist_ok=True)
    stats = Stats(segments=len(segments))
    pool = EDGE_SPEAKER_VOICES.get(lang, EDGE_SPEAKER_VOICES["zh"])
    by_profile = EDGE_VOICE_BY_PROFILE.get(lang, {})
    for i, s in enumerate(segments):
        dst = os.path.join(out_dir, f"seg_{i:04d}.wav")
        text = s.translated or s.text
        if backend == "stub":
            _tts_stub(text, dst, s.duration)
        elif backend == "edge":
            v = voice
            if not v and profiles and s.speaker in profiles:
                p = profiles[s.speaker]
                v = by_profile.get((p["gender"], p["age"]), "")
            _tts_edge(text, dst, v or pool[s.speaker % len(pool)])
        elif backend == "cosyvoice":
            _tts_cosyvoice(text, dst, ref_wav, device)
        else:
            raise ValueError(f"未知 TTS backend: {backend}")
        s.tts_path = dst
    return stats


def _tts_stub(text: str, dst: str, duration: float) -> None:
    """占位音：一声轻响，用来验证时间轴，不是给人听的。"""
    dur = max(0.4, min(duration if duration > 0 else len(text) / 4.5, 30.0))
    n = int(dur * SR_TTS)
    t = np.arange(n) / SR_TTS
    tone = 0.08 * np.sin(2 * np.pi * 440 * t) * np.exp(-3 * t)
    audio.save(dst, tone.astype(np.float32), SR_TTS)


def _tts_edge(text: str, dst: str, voice: str) -> None:
    import asyncio

    import edge_tts

    async def run():
        await edge_tts.Communicate(text, voice).save(dst)

    # 微软端点偶发 403/断连，重试三次基本必过
    for attempt in range(3):
        try:
            asyncio.run(run())
            return
        except Exception as exc:
            if attempt == 2:
                raise
            print(f"    [tts] edge 重试 {attempt + 1}/3：{type(exc).__name__}", flush=True)
            time.sleep(1.5 * (attempt + 1))


_COSY = {}


def _tts_cosyvoice(text: str, dst: str, ref_wav: str, device: str) -> None:
    root = os.environ.get("DUB_COSYVOICE_DIR", "")
    model_dir = os.environ.get("DUB_COSYVOICE_MODEL", "")
    if not root or not model_dir:
        raise RuntimeError("需要 DUB_COSYVOICE_DIR 与 DUB_COSYVOICE_MODEL")
    if "m" not in _COSY:
        if root not in sys.path:
            sys.path.insert(0, root)
        from cosyvoice.cli.cosyvoice import CosyVoice2

        _COSY["m"] = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)
    model = _COSY["m"]
    if not ref_wav:
        raise RuntimeError("CosyVoice 需要 --ref-wav 作为音色参考")
    chunks = []
    for out in model.inference_cross_lingual(text, ref_wav, stream=False):
        chunks.append(out["tts_speech"].squeeze(0).numpy())
    wav = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    audio.save(dst, wav, model.sample_rate)


# --------------------------------------------------------------------------- #
# 6. 对齐 + 混音
# --------------------------------------------------------------------------- #


def assemble(bed_wav: str, segments: list[Segment], out_wav: str,
             duck_db: float = -8.0, max_stretch: float = 1.25) -> Stats:
    stats = Stats(segments=len(segments))
    bed, sr = audio.load(bed_wav, sr=SR_MIX)
    if bed.shape[1] == 1:
        bed = np.repeat(bed, 2, axis=1)
    n = len(bed)
    dub = np.zeros((n, 2), dtype=np.float32)

    for s in segments:
        if not s.tts_path or not os.path.exists(s.tts_path):
            continue
        clip, csr = audio.load(s.tts_path, sr=SR_MIX, mono=False)
        if clip.shape[1] == 1:
            clip = np.repeat(clip, 2, axis=1)
        target = s.duration
        if target > 0 and len(clip) / SR_MIX > target * 1.05:
            rate = min(max_stretch, (len(clip) / SR_MIX) / target)
            clip = audio.time_stretch(clip, SR_MIX, rate)
            stats.overflow += 1
        start = int(s.start * SR_MIX)
        audio.place_fade(dub, clip, start)

    spans = [(int(s.start * SR_MIX), int(s.end * SR_MIX)) for s in segments]
    env = audio.db_envelope(n, spans, SR_MIX, duck_db)
    mix = bed * env[:, None] + dub
    mix = audio.peak_normalize(mix, -1.0)
    audio.save(out_wav, mix, SR_MIX)
    return stats
