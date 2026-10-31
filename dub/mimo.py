"""小米 MiMo 云端 TTS 客户端（OpenAI 兼容 /chat/completions）。

调用格式同小米官方文档（mimo.mi.com）：文本放 role=assistant 消息，
audio.format=wav + voice → choices[0].message.audio.data（b64 WAV，24kHz）。
实现移植自 codevid 项目里真机验证过的 mimo_voice.py；本模块只管单句合成，
配置解析（key 三通道）在 serve.py，与 LLM 配置同一套模式。
"""
from __future__ import annotations

import base64
import json
import urllib.request

DEFAULT_BASE = "https://api.xiaomimimo.com/v1"
TTS_MODEL = "mimo-v2.5-tts"
TIMEOUT = 90

# 可用音色（400 报错会列出）：mimo_default, 冰糖, 茉莉, 苏打, 白桦, Mia, Chloe, Milo, Dean
# 中文可靠：茉莉/苏打/白桦；Chloe 中文发音糊勿用。
VOICES = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]


class MimoError(Exception):
    pass


def tts_one(base: str, key: str, text: str, voice: str, dst: str, timeout: int = TIMEOUT) -> None:
    """单句合成 → WAV 文件。抛 MimoError/URLError 由调用方重试。"""
    payload = {
        "model": TTS_MODEL,
        "messages": [{"role": "assistant", "content": text}],
        "audio": {"format": "wav", "voice": voice},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        wav = base64.b64decode(data["choices"][0]["message"]["audio"]["data"])
    except (KeyError, IndexError, TypeError) as exc:
        raise MimoError(f"TTS 响应缺少音频字段: {exc}") from None
    with open(dst, "wb") as f:
        f.write(wav)
