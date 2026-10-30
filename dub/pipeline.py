"""端到端编排：每一步都有缓存，坏了只重跑坏的那段。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from . import audio, stages
from .stages import Segment, Stats


# --force 与缓存判断用的短名 → 实际落盘文件名
CACHE_FILES = {
    "mix": "mix.wav",
    "bed": "bed.wav",   # vocals.wav 随 bed 一起生成
    "asr": "asr.json",
    "trans": "trans.json",
    "tts": "tts",       # 目录
    "final": "final.wav",
}


@dataclass
class Options:
    input: str
    out: str = "out/final.wav"
    lang: str = "zh"
    source_lang: str = ""
    asr_backend: str = "auto"
    asr_model: str = "large-v3-turbo"
    mt_backend: str = "stub"
    tts_backend: str = "stub"
    voice: str = ""
    ref_wav: str = ""
    device: str = "auto"
    separate: bool = True
    duck_db: float = -8.0
    chars_per_sec: float = 4.5
    workdir: str = "work"
    force: list[str] = field(default_factory=list)
    api_base: str = ""
    api_key: str = ""
    llm_model: str = ""


def _key(opts: Options) -> str:
    st = os.stat(opts.input)
    raw = json.dumps(
        {
            "input": os.path.abspath(opts.input),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "lang": opts.lang,
            "asr": opts.asr_backend,
            "asr_model": opts.asr_model,
            "mt": opts.mt_backend,
            "tts": opts.tts_backend,
            "voice": opts.voice,
            "sep": opts.separate,
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class Runner:
    def __init__(self, opts: Options, on_stage=None):
        self.o = opts
        self.work = os.path.join(opts.workdir, _key(opts))
        os.makedirs(self.work, exist_ok=True)
        self.timings: dict[str, float] = {}
        self.profiles: dict = {}
        self.on_stage = on_stage  # on_stage(name, "start"|"done", dt)

    def _t(self, name: str, fn):
        t0 = time.perf_counter()
        print(f"[{name}] ...", flush=True)
        if self.on_stage:
            self.on_stage(name, "start", 0.0)
        out = fn()
        dt = time.perf_counter() - t0
        self.timings[name] = dt
        print(f"[{name}] {dt:.1f}s", flush=True)
        if self.on_stage:
            self.on_stage(name, "done", dt)
        return out

    def _path(self, name: str) -> str:
        return os.path.join(self.work, name)

    def _need(self, name: str) -> bool:
        path = self._path(CACHE_FILES.get(name, name))
        return name in self.o.force or not os.path.exists(path)

    # ------------------------------------------------------------------ #

    def mix(self) -> str:
        p = self._path("mix.wav")
        if self._need("mix"):
            dur = self._t("extract", lambda: stages.extract_audio(self.o.input, p))
            print(f"    时长 {dur:.1f}s")
        return p

    def vocals(self) -> tuple[str, str, bool]:
        """返回 (vocals, bed, separated)。分离失败时退回原混音 + duck。"""
        mixp = self.mix()
        v, b = self._path("vocals.wav"), self._path("bed.wav")
        if not self.o.separate:
            return mixp, mixp, False
        if self._need("bed"):
            try:
                self._t("separate", lambda: stages.separate(mixp, self.work, device=self.o.device))
                separated = True
            except Exception as exc:
                print(f"[warn] 分离失败，退回 duck 模式：{type(exc).__name__}: {exc}")
                return mixp, mixp, False
        return v, b, True

    def asr(self, audio_path: str) -> list[Segment]:
        p = self._path("asr.json")
        if self._need("asr"):
            segs = self._t(
                "asr",
                lambda: stages.asr(
                    audio_path,
                    backend=self.o.asr_backend,
                    model=self.o.asr_model,
                    language=self.o.source_lang or None,
                    device=self.o.device,
                ),
            )
            json.dump([s.to_dict() for s in segs], open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.remove(self._path("profiles.json")) if os.path.exists(self._path("profiles.json")) else None
        else:
            segs = [Segment.from_dict(d) for d in json.load(open(p, encoding="utf-8"))]
        print(f"    片段 {len(segs)}")
        self.profiles = self._profiles(audio_path, segs)
        return segs

    def _profiles(self, vocals_path: str, segs: list[Segment]) -> dict:
        """说话人声学画像（性别/年龄）。缓存于 profiles.json，随 asr 失效。"""
        p = self._path("profiles.json")
        if os.path.exists(p):
            return {int(k): v for k, v in json.load(open(p, encoding="utf-8")).items()}
        from .profile import speaker_profiles

        prof = speaker_profiles(vocals_path, segs)
        json.dump(prof, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return prof

    def translate(self, segs: list[Segment]) -> Stats:
        p = self._path("trans.json")
        if self._need("trans"):
            st = self._t(
                "translate",
                lambda: stages.translate(
                    segs,
                    backend=self.o.mt_backend,
                    target=self.o.lang,
                    chars_per_sec=self.o.chars_per_sec,
                    api_base=self.o.api_base,
                    api_key=self.o.api_key,
                    model=self.o.llm_model,
                ),
            )
            json.dump([s.to_dict() for s in segs], open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        else:
            segs[:] = [Segment.from_dict(d) for d in json.load(open(p, encoding="utf-8"))]
            st = Stats(segments=len(segs))
        return st

    def tts(self, segs: list[Segment]) -> None:
        d = self._path("tts")
        if self._need("tts"):
            self._t(
                "tts",
                lambda: stages.tts(
                    segs, d, backend=self.o.tts_backend, voice=self.o.voice,
                    device=self.o.device, ref_wav=self.o.ref_wav, lang=self.o.lang,
                    profiles=getattr(self, "profiles", None),
                ),
            )
        else:
            for i, s in enumerate(segs):
                s.tts_path = os.path.join(d, f"seg_{i:04d}.wav")

    def assemble(self, bed: str, segs: list[Segment]) -> Stats:
        p = self._path("final.wav")
        if self._need("final"):
            st = self._t(
                "assemble",
                lambda: stages.assemble(bed, segs, p, duck_db=self.o.duck_db),
            )
        else:
            st = Stats(segments=len(segs))
        os.makedirs(os.path.dirname(os.path.abspath(self.o.out)) or ".", exist_ok=True)
        import shutil

        shutil.copyfile(p, self.o.out)
        return st

    # ------------------------------------------------------------------ #

    def run(self) -> dict:
        print(f"工作目录: {self.work}")
        vocals, bed, separated = self.vocals()
        print(f"分离: {'是' if separated else '否（duck 兜底）'}")
        segs = self.asr(vocals)
        st = self.translate(segs)
        self.tts(segs)
        ast = self.assemble(bed, segs)

        summary = {
            "input": self.o.input,
            "out": self.o.out,
            "workdir": self.work,
            "segments": len(segs),
            "translated_chars": st.translated_chars,
            "overflow": ast.overflow,
            "separated": separated,
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
            "total_s": round(sum(self.timings.values()), 2),
        }
        json.dump(summary, open(self._path("summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return summary
