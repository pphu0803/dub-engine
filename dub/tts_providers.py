"""TTS Provider 抽象层：内置 edge / openai / mimo / cosyvoice / stub，config 可扩展。

设计边界：每个 provider 只负责「单句合成 → dst.wav」与测活试听；批量并发、
画像→声线解析（voice_maps 池化轮转）、升降 key 后处理都在 stages.tts 统一做。
新增云厂商两条路：
  1. 零代码：厂商有 OpenAI 兼容 /v1/audio/speech 端点 → config 里配一个
     {"type": "openai"} 的 provider 即可；
  2. 小适配器：特殊协议（如小米的 chat/completions 格式）→ 写一个 ~50 行
     子类注册进 PROVIDER_TYPES。
"""
from __future__ import annotations

import copy


class TTSProvider:
    type = ""
    concurrency = 1  # 网络型 6；本地模型型 1

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def synthesize(self, text: str, voice: str, dst: str) -> None:
        raise NotImplementedError

    def fallback_voice(self, lang: str) -> tuple[str, float]:
        """voice_maps 完全没命中时的兜底声线。"""
        raise NotImplementedError

    def test(self, voice: str, text: str = "") -> dict:
        """合成一句短文本 → {ok, latency_ms, audio_b64?, error?}（管理页试听）。"""
        import base64
        import os
        import tempfile
        import time

        text = text or "你好，这是配音引擎的语音测试。"
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        t0 = time.perf_counter()
        try:
            self.synthesize(text, voice, tmp)
            with open(tmp, "rb") as f:
                wav = f.read()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000),
                    "audio_b64": base64.b64encode(wav).decode("ascii")}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _retry(fn, what: str, attempts: int = 3):
    """云端 TTS 偶发 403/断连/超时，逐段退避重试。"""
    import time

    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            wait = 1.5 * (attempt + 1)
            print(f"    [tts] {what} 重试 {attempt + 1}/{attempts}：{type(exc).__name__}，{wait}s 后",
                  flush=True)
            time.sleep(wait)


def _to_wav(data: bytes, ar: int = 24000) -> bytes:
    """任意音频容器 → wav（兼容站可能无视 response_format）。"""
    import subprocess

    import imageio_ffmpeg

    p = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error",
         "-i", "pipe:", "-f", "wav", "-ac", "1", "-ar", str(ar), "pipe:"],
        input=data, capture_output=True, timeout=120)
    p.check_returncode()
    return p.stdout


class EdgeProvider(TTSProvider):
    """微软 Edge 在线朗读（免 key，覆盖中/英/日/韩）。"""

    type = "edge"
    concurrency = 6

    def synthesize(self, text: str, voice: str, dst: str) -> None:
        import asyncio

        import edge_tts

        _retry(lambda: asyncio.run(edge_tts.Communicate(text, voice).save(dst)), "edge")

    def fallback_voice(self, lang: str) -> tuple[str, float]:
        return (EDGE_DEFAULT_VOICES.get(lang, EDGE_DEFAULT_VOICES["zh"]), 0.0)


class OpenAIProvider(TTSProvider):
    """OpenAI 兼容 /v1/audio/speech——覆盖 OpenAI/硅基流动/各类聚合站。"""

    type = "openai"
    concurrency = 6

    def synthesize(self, text: str, voice: str, dst: str) -> None:
        import requests

        cfg = self.cfg
        base = (cfg.get("base") or "https://api.openai.com/v1").rstrip("/")
        if not cfg.get("key"):
            raise ValueError("openai 兼容 TTS 缺少 key")
        model = cfg.get("model") or "gpt-4o-mini-tts"

        def one():
            sess = requests.Session()
            sess.trust_env = False  # 与翻译管线同款直连
            r = sess.post(f"{base}/audio/speech",
                          headers={"Authorization": f"Bearer {cfg['key']}"},
                          json={"model": model, "voice": voice, "input": text,
                                "response_format": "wav"},
                          timeout=(15, 120))
            r.raise_for_status()
            data = r.content
            if data[:4] != b"RIFF":  # 兼容站可能无视 response_format，统一转 wav
                data = _to_wav(data)
            with open(dst, "wb") as f:
                f.write(data)

        _retry(one, "openai")

    def fallback_voice(self, lang: str) -> tuple[str, float]:
        return ("alloy", 0.0)


class MimoProvider(TTSProvider):
    """小米 MiMo 云端 TTS（chat/completions 特殊格式，见 dub/mimo.py）。"""

    type = "mimo"
    concurrency = 6

    def synthesize(self, text: str, voice: str, dst: str) -> None:
        from . import mimo

        cfg = self.cfg

        _retry(lambda: mimo.tts_one(cfg.get("base") or mimo.DEFAULT_BASE,
                                    cfg.get("key", ""), text, voice, dst,
                                    model=cfg.get("model") or mimo.TTS_MODEL), "mimo")

    def fallback_voice(self, lang: str) -> tuple[str, float]:
        return ("mimo_default", 0.0)


class CosyVoiceProvider(TTSProvider):
    """本地 CosyVoice2 声音克隆（parked：产出质量问题见 README 已知问题）。

    配置：{"type":"cosyvoice", "root": 仓库路径, "model": 模型目录, "ref_wav": 参考音频}
    环境变量 DUB_COSYVOICE_DIR / DUB_COSYVOICE_MODEL 兼容旧入口。"""

    type = "cosyvoice"
    concurrency = 1

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import os

        self.root = self.cfg.get("root") or os.environ.get("DUB_COSYVOICE_DIR", "")
        self.model = self.cfg.get("model") or os.environ.get("DUB_COSYVOICE_MODEL", "")
        self.ref_wav = self.cfg.get("ref_wav", "")
        self._m = None

    def synthesize(self, text: str, voice: str, dst: str) -> None:
        import sys

        import numpy as np

        from . import audio

        if not self.root or not self.model:
            raise RuntimeError("cosyvoice 需要 root/model（config 或 DUB_COSYVOICE_*）")
        if not self.ref_wav:
            raise RuntimeError("cosyvoice 需要 ref_wav 参考音频")
        if self._m is None:
            if self.root not in sys.path:
                sys.path.insert(0, self.root)
            from cosyvoice.cli.cosyvoice import CosyVoice2

            self._m = CosyVoice2(self.model, load_jit=False, load_trt=False, fp16=False)
        chunks = []
        for out in self._m.inference_cross_lingual(text, self.ref_wav, stream=False):
            chunks.append(out["tts_speech"].squeeze(0).numpy())
        wav = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        audio.save(dst, wav, self._m.sample_rate)

    def fallback_voice(self, lang: str) -> tuple[str, float]:
        return (self.ref_wav or "ref", 0.0)


class StubProvider(TTSProvider):
    """占位音：一声轻响，验证时间轴用，不是给人听的。"""

    type = "stub"
    concurrency = 1

    def synthesize(self, text: str, voice: str, dst: str) -> None:
        import numpy as np

        from . import audio
        from .stages import SR_TTS

        dur = max(0.4, min(len(text) / 4.5, 30.0))
        n = int(dur * SR_TTS)
        t = np.arange(n) / SR_TTS
        tone = 0.08 * np.sin(2 * np.pi * 440 * t) * np.exp(-3 * t)
        audio.save(dst, tone.astype(np.float32), SR_TTS)

    def fallback_voice(self, lang: str) -> tuple[str, float]:
        return ("stub", 0.0)


PROVIDER_TYPES = {
    cls.type: cls for cls in
    (EdgeProvider, OpenAIProvider, MimoProvider, CosyVoiceProvider, StubProvider)
}

BUILTIN_PROVIDERS = ("edge", "stub", "cosyvoice", "mimo", "openai")


def build_provider(ptype: str, cfg: dict | None = None) -> TTSProvider:
    """按 type 构造 provider。mimo 的 key/base 支持 DUB_MIMO_* 环境变量兜底
    （CLI/pipeline 路径的三通道语义）。"""
    import os

    cls = PROVIDER_TYPES.get(ptype)
    if not cls:
        raise ValueError(f"未知 TTS provider 类型: {ptype}（可用: {', '.join(PROVIDER_TYPES)}）")
    cfg = dict(cfg or {})
    if ptype == "mimo":
        cfg.setdefault("base", os.environ.get("DUB_MIMO_BASE", "") or None)
        cfg.setdefault("key", os.environ.get("DUB_MIMO_KEY", "") or None)
        cfg = {k: v for k, v in cfg.items() if v}
    return cls(cfg)


# ---------------------------------------------------------------- 声线映射数据
# 结构：{provider: {lang: {"m_young": [(声线, 半音)...], ..., "*": 兜底池}}}
# 半音非 0 = 合成后升降 key（变调不变速；负值男声低沉显沧桑）。
# 同池多声线按说话人编号轮转——同画像角色不再塌成同一声线。
# config.json tts.voice_maps 同键深合并覆盖这里的内置默认。

EDGE_DEFAULT_VOICES = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "zh-male": "zh-CN-YunxiNeural",
    "en": "en-US-AriaNeural",
    "en-male": "en-US-GuyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ja-male": "ja-JP-KeitaNeural",
    "ko": "ko-KR-SunHiNeural",
    "ko-male": "ko-KR-InJoonNeural",
}

# 说话人序号兜底池（无画像/画像缺组合时轮转）；ja/ko 可用声线少（实测），池小但男女有别
_EDGE_POOLS = {
    "zh": ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunyangNeural"],
    "en": ["en-US-AriaNeural", "en-US-GuyNeural", "en-US-MichelleNeural", "en-US-RogerNeural"],
    "ja": ["ja-JP-NanamiNeural", "ja-JP-KeitaNeural"],
    "ko": ["ko-KR-SunHiNeural", "ko-KR-InJoonNeural", "ko-KR-HyunsuMultilingualNeural"],
}


def _edge_map(profiles: dict, pools: dict) -> dict:
    def key(k):
        return k if isinstance(k, str) else f"{k[0]}_{k[1]}"

    m = {lang: {key(k): [(v, 0.0)] for k, v in tbl.items()}
         for lang, tbl in profiles.items()}
    for lang, pool in pools.items():
        m.setdefault(lang, {})["*"] = [(v, 0.0) for v in pool]
    return m


BUILTIN_VOICE_MAPS = {
    # 声学画像 → 音色（性别/年龄必须对得上，老年男不配年轻女）。
    # 六种 (gender, age) 组合全覆盖；ja/ko 没有童声，child 就近落 young。
    "edge": _edge_map(
        {
            "zh": {
                ("m", "child"): "zh-CN-YunxiaNeural",
                ("m", "young"): "zh-CN-YunxiNeural",
                ("m", "mature"): "zh-CN-YunyangNeural",
                ("f", "child"): "zh-CN-XiaoxiaoNeural",
                ("f", "young"): "zh-CN-XiaoxiaoNeural",
                ("f", "mature"): "zh-CN-XiaoyiNeural",
                ("x", "child"): "zh-CN-XiaoxiaoNeural",
                ("x", "young"): "zh-CN-XiaoxiaoNeural",
                ("x", "mature"): "zh-CN-XiaoxiaoNeural",
            },
            "en": {
                ("m", "child"): "en-US-GuyNeural",
                ("m", "young"): "en-US-AndrewNeural",
                ("m", "mature"): "en-US-RogerNeural",
                ("f", "child"): "en-US-AnaNeural",
                ("f", "young"): "en-US-AriaNeural",
                ("f", "mature"): "en-US-MichelleNeural",
                ("x", "child"): "en-US-AriaNeural",
                ("x", "young"): "en-US-AriaNeural",
                ("x", "mature"): "en-US-AriaNeural",
            },
            "ja": {
                ("m", "child"): "ja-JP-KeitaNeural",
                ("m", "young"): "ja-JP-KeitaNeural",
                ("m", "mature"): "ja-JP-KeitaNeural",
                ("f", "child"): "ja-JP-NanamiNeural",
                ("f", "young"): "ja-JP-NanamiNeural",
                ("f", "mature"): "ja-JP-NanamiNeural",
                ("x", "child"): "ja-JP-NanamiNeural",
                ("x", "young"): "ja-JP-NanamiNeural",
                ("x", "mature"): "ja-JP-NanamiNeural",
            },
            "ko": {
                ("m", "child"): "ko-KR-HyunsuMultilingualNeural",
                ("m", "young"): "ko-KR-HyunsuMultilingualNeural",
                ("m", "mature"): "ko-KR-InJoonNeural",
                ("f", "child"): "ko-KR-SunHiNeural",
                ("f", "young"): "ko-KR-SunHiNeural",
                ("f", "mature"): "ko-KR-SunHiNeural",
                ("x", "child"): "ko-KR-SunHiNeural",
                ("x", "young"): "ko-KR-SunHiNeural",
                ("x", "mature"): "ko-KR-SunHiNeural",
            },
        },
        _EDGE_POOLS,
    ),
    # 2026-11-01 用户耳朵标注：zh 苏打/白桦=年轻男、冰糖=年轻女、茉莉=女沉稳(mature)、
    # mimo_default=女(AI 感强，只做兜底)；en Mia/Chloe=年轻女、Milo=年轻男、Dean=成熟男。
    # zh 没有成熟男声线 → 白桦-4st(≈55岁)/苏打-3st(≈40岁) 降 key 代偿（用户 v2 复听定稿）。
    "mimo": {
        "zh": {
            "m_child": [("苏打", 0.0), ("白桦", 0.0)],
            "m_young": [("苏打", 0.0), ("白桦", 0.0)],
            "m_mature": [("白桦", -4.0), ("苏打", -3.0)],
            "f_child": [("冰糖", 0.0)],
            "f_young": [("冰糖", 0.0)],
            "f_mature": [("茉莉", 0.0)],
            "*": [("mimo_default", 0.0)],
        },
        "en": {
            "m_child": [("Milo", 0.0)],
            "m_young": [("Milo", 0.0)],
            "m_mature": [("Dean", 0.0)],
            "f_child": [("Chloe", 0.0)],
            "f_young": [("Mia", 0.0), ("Chloe", 0.0)],
            "f_mature": [("Mia", 0.0), ("Chloe", 0.0)],
            "*": [("mimo_default", 0.0)],
        },
    },
}


def builtin_maps(provider: str) -> dict:
    """内置声线映射（深拷贝，调用方随便改）。"""
    return copy.deepcopy(BUILTIN_VOICE_MAPS.get(provider, {}))


def merge_voice_maps(provider: str, user: dict | None) -> dict:
    """内置映射 + 用户 config 覆盖（按 lang 整表替换；用户没配的 lang 用内置）。"""
    base = builtin_maps(provider)
    for lang, tbl in ((user or {}).get(provider) or {}).items():
        base[lang] = {**base.get(lang, {}), **tbl}
    return base
