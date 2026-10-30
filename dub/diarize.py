"""说话人分离：CAM++ 嵌入 + 凝聚聚类。段级粒度（一段台词一个说话人）。

阈值依据：同说话人 cos ~0.7、异说话人 ~0（官方 example 实测），
取余弦距离 0.55 做切割，正负样本间隔大，不敏感。
"""

from __future__ import annotations

from . import audio

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        import os

        import torch

        from .campplus import CAMPPlus

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "models", "campplus_cn_common.bin")
        if not os.path.exists(path):
            raise RuntimeError(f"CAM++ 权重不存在：{path}（ModelScope iic/speech_campplus_sv_zh-cn_16k-common）")
        m = CAMPPlus(feat_dim=80, embedding_size=192)
        m.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
        m.eval()
        _MODEL = m
    return _MODEL


def embed(wav16k_mono) -> list[float]:
    """单段音频 → 归一化嵌入。wav16k_mono: (n,) float32。"""
    import torch
    import torchaudio.compliance.kaldi as kaldi

    f = kaldi.fbank(torch.from_numpy(wav16k_mono).unsqueeze(0),
                    num_mel_bins=80, sample_frequency=16000, use_energy=False)
    f = f - f.mean(0, keepdim=True)
    with torch.no_grad():
        e = _model()(f.unsqueeze(0))
    e = torch.nn.functional.normalize(e.flatten(), dim=0)
    return e.tolist()


def diarize(vocals_path: str, segments: list) -> list:
    """给每个 Segment 标 speaker（0 起）。单说话人时全 0。"""
    from scipy.cluster.hierarchy import fcluster, linkage

    import numpy as np

    data, sr = audio.load(vocals_path, sr=16000, mono=True)
    embs = []
    for s in segments:
        seg = data[int(s.start * sr):int(s.end * sr), 0]
        if len(seg) < sr:  # <1s 嵌入不稳，抄上一段
            embs.append(embs[-1] if embs else None)
            continue
        embs.append(embed(seg))
    if any(e is None for e in embs):  # 开头就有超短段的极端情况
        filler = next((e for e in embs if e is not None), [0.0] * 192)
        embs = [filler if e is None else e for e in embs]

    x = np.array(embs)
    n_speakers = len(segments)
    if len(segments) > 1:
        z = linkage(x, method="average", metric="cosine")
        labels = fcluster(z, t=0.55, criterion="distance")
        # 层次聚类标签按首现顺序重编号，保证 speaker 0 = 最先开口的人
        order, remap = [], {}
        for lb in labels:
            if lb not in remap:
                remap[lb] = len(order)
                order.append(lb)
        n_speakers = len(remap)
        labels = [remap[lb] for lb in labels]
    else:
        labels = [0]
    for s, lb in zip(segments, labels):
        s.speaker = int(lb)
    print(f"    [diarize] {n_speakers} 个说话人")
    return segments
