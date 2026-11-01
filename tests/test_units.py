"""Offline unit tests (CPU only, no GPU / network): P1/P2/P3 quality-fix logic.

Run:  python -m pytest tests/ -q
Covers:
- T1  sentence splitting snaps to real ASR anchors (_split_long, P3)
- T2  speaker profile consolidation (SpeakerRegistry.consolidate, P2)
- T3  original-audio replay outside dubbed spans (_mix_chunk voc_path, P1)
"""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dub import audio  # noqa: E402
from dub.stages import Segment, _split_long  # noqa: E402
from dub.streaming import SpeakerRegistry, _mix_chunk  # noqa: E402


def test_t1_split_snaps_to_anchors():
    long = Segment(0.0, 60.0,
                   "第一句话说得比较长一些。第二句说得很长很长很长很长。第三句话也是相当的长。第四句也长一些一些一些一些。")
    long.anchors = [14.8, 31.2, 47.5]
    out = _split_long([long])
    assert len(out) >= 3, f"should split into >=3 sentences, got {len(out)}"
    ends = [s.end for s in out[:-1]]
    hits = sum(1 for t in (14.8, 31.2, 47.5) if any(abs(t - e) < 0.01 for e in ends))
    print(f"T1 split: {len(out)} sentences, ends {[round(x, 2) for x in ends]}, anchors hit {hits}/3")
    assert hits >= 2, "at least 2 boundaries should snap to real anchors"

    # no anchors -> pure ratio fallback, must not crash
    plain = Segment(0.0, 30.0, "甲句说得非常长。乙句也说得非常长。丙句还是说得非常长。")
    assert len(_split_long([plain])) >= 3


def test_t2_speaker_consolidation():
    reg = SpeakerRegistry()
    reg.centroids = [np.eye(192, dtype=np.float32)[i % 192] for i in range(5)]
    reg.profiles = {
        0: {"gender": "f", "age": "young", "f0": 220.0, "f0std": 20, "centroid": 1800},
        1: {"gender": "m", "age": "mature", "f0": 130.0, "f0std": 30, "centroid": 1500},
        2: {"gender": "f", "age": "young", "f0": 205.0, "f0std": 25, "centroid": 1750},
        3: {"gender": "f", "age": "young", "f0": 212.0, "f0std": 22, "centroid": 1780},
        4: {"gender": "x", "age": "young", "f0": 0.0, "f0std": 0, "centroid": 1000},
    }
    mapping = reg.consolidate()
    classes = set(mapping.values())
    print(f"T2 consolidate: 5 IDs -> {len(classes)} classes, mapping={mapping}")
    assert len(classes) == 3, "f/young trio merges into 1 class + male 1 + x 1"
    assert mapping[0] == mapping[2] == mapping[3]
    assert mapping[1] != mapping[0] and mapping[4] != mapping[0]
    assert len(reg.centroids) == 3 and 0 in reg.profiles
    # post-merge profile is the recomputed class profile
    assert reg.profiles[0]["gender"] == "f" and 190 < reg.profiles[0]["f0"] < 225


def test_t3_original_audio_replay_outside_spans():
    sr = 44100
    t = np.arange(30 * sr, dtype=np.float32) / sr
    bed = (0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    voc = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    td = np.arange(5 * sr, dtype=np.float32) / sr  # dub clip semantics: starts at sentence begin
    dub = (0.5 * np.sin(2 * np.pi * 660 * td)).astype(np.float32)

    tmp = tempfile.mkdtemp(prefix="dub_test_p1_")
    for name, data in [("bed.wav", bed), ("voc.wav", voc), ("dub.wav", dub)]:
        audio.save(os.path.join(tmp, name), np.stack([data, data], 1), sr)
    seg = Segment(5.0, 10.0, "x")
    seg.tts_path = os.path.join(tmp, "dub.wav")
    out_p = os.path.join(tmp, "mix.wav")
    _mix_chunk(os.path.join(tmp, "bed.wav"), [seg], out_p, gain=1.0,
               voc_path=os.path.join(tmp, "voc.wav"))
    m, _ = audio.load(out_p, sr=sr)

    def amp(x, sec):
        w = x[int(sec * sr):int(sec * sr) + sr // 10]  # 100ms RMS window
        return float(np.sqrt(np.mean(w ** 2)))

    a_out, a_in = amp(m, 1.0), amp(m, 7.0)
    print(f"T3 mix: outside RMS {a_out:.3f} (want ~0.28=original), inside {a_in:.3f} (want ~0.36=dub)")
    assert a_out > 0.2, "original vocals must be audible outside dubbed spans (no silent holes)"
    assert a_in > 0.3 and a_in > a_out, "dub must dominate inside the covered span"
