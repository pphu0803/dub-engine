"""命令行入口：python -m dub <doctor|run>"""

from __future__ import annotations

import argparse
import json
import sys

from .doctor import run as doctor_run
from .pipeline import Options, Runner


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dub", description="网页视频 AI 配音离线管线")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="检查环境与依赖")

    s = sub.add_parser("serve", help="启动本地服务（浏览器插件后端）")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--llm-base", default="", help="OpenAI 兼容接口地址（如 https://api.deepseek.com/v1）")
    s.add_argument("--llm-key", default="", help="LLM API key（也可用 config.json 或环境变量）")
    s.add_argument("--llm-model", default="", help="模型名（如 deepseek-chat）")

    r = sub.add_parser("run", help="跑完整管线")
    r.add_argument("--input", required=True, help="本地媒体文件（mp4/mkv/m4s/wav 都行）")
    r.add_argument("--out", default="out/final.wav")
    r.add_argument("--lang", default="zh", choices=["zh", "en", "ja", "ko"])
    r.add_argument("--source-lang", default="", help="源语言，留空自动判断")
    r.add_argument("--asr", dest="asr_backend", default="auto", choices=["auto", "stub", "faster", "transformers"])
    r.add_argument("--asr-model", default="large-v3-turbo")
    r.add_argument("--mt", dest="mt_backend", default="stub", choices=["stub", "llm"])
    r.add_argument("--llm-model", default="")
    r.add_argument("--api-base", default="")
    r.add_argument("--api-key", default="")
    r.add_argument("--tts", dest="tts_backend", default="stub", choices=["stub", "edge", "cosyvoice"])
    r.add_argument("--voice", default="", help="edge 音色名，如 zh-CN-XiaoxiaoNeural")
    r.add_argument("--ref-wav", default="", help="CosyVoice 的音色参考音频")
    r.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    r.add_argument("--no-separate", action="store_true", help="跳过人声分离，直接 duck")
    r.add_argument("--duck-db", type=float, default=-8.0)
    r.add_argument("--chars-per-sec", type=float, default=4.5)
    r.add_argument("--workdir", default="work")
    r.add_argument("--force", default="", help="逗号分隔：mix,bed,asr,trans,tts,final")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "doctor":
        return doctor_run()
    if args.cmd == "serve":
        from .serve import serve

        serve(args.port, args.llm_base, args.llm_key, args.llm_model)
        return 0

    opts = Options(
        input=args.input,
        out=args.out,
        lang=args.lang,
        source_lang=args.source_lang,
        asr_backend=args.asr_backend,
        asr_model=args.asr_model,
        mt_backend=args.mt_backend,
        tts_backend=args.tts_backend,
        voice=args.voice,
        ref_wav=args.ref_wav,
        device=args.device,
        separate=not args.no_separate,
        duck_db=args.duck_db,
        chars_per_sec=args.chars_per_sec,
        workdir=args.workdir,
        force=[x for x in args.force.split(",") if x],
        api_base=args.api_base,
        api_key=args.api_key,
        llm_model=args.llm_model,
    )
    summary = Runner(opts).run()
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
