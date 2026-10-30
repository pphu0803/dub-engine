# dub-engine

网页视频 AI 配音引擎。当前进度：**音轨预取已验证 → 离线管线骨架跑通 → Demucs 在 ROCm 上跑通**。

## 现在能做什么

```
取音轨 → 分离人声/伴奏 → 识别 → 翻译 → 合成 → 对齐混音 → 成品 wav
  ✅        ✅ Demucs       ⏳      ⏳     ⏳        ✅
```

- ✅ 已跑通：音轨预取（B 站实测 54 MB / 4 s）、解码、Demucs 分离（ROCm，2× 实时）、ducking 混音、整条管线的缓存与编排。
- ⏳ 待接：ASR（`transformers` 后端已写好，未跑真模型）、翻译（LLM 接口已写好，缺 key）、TTS（CosyVoice 适配器已写，未装模型）。
- 🧪 全链路可以用 `--asr stub --mt stub --tts stub` 跑通，验证时间轴和混音。

## 目录

```
dub/
  pipeline.py   编排 + 缓存（按输入哈希分目录，哪步坏了只重跑那步）
  stages.py     extract / separate / asr / translate / tts / assemble
  audio.py      读写、重采样、time-stretch、ducking 包络
  doctor.py     环境自检
  cli.py        命令行
tools/
  probe_media.py        测音轨获取速度
  decode_audio.py       媒体 → PCM
  make_clip.py          切测试素材
  browser/collect.js    在视频页找音轨地址
  remote/*.ps1          远程机器部署脚本
```

---

## 在 rog-windows 上跑

### 机器事实（2026-10 实测）

```
CPU   AMD Ryzen AI MAX+ 395  16C/32T
RAM   123.6 GB 统一内存
GPU   Radeon 8060S（gfx1151），ROCm 7.2，torch 可见 78.2 GB 显存
```

### 两个 Python，别搞混

| 用途 | 解释器 | 说明 |
|---|---|---|
| **主环境**（ROCm torch、numpy、soundfile、librosa、av、transformers、edge-tts） | `H:\SHARE\ComfyUI\miniconda3\envs\comfyui\python.exe` | ComfyUI 的 conda 环境，自带 ROCm 版 torch |
| **装 demucs 的环境** | `C:\dub\venv\Scripts\python.exe` | 从上面那个 venv 出来，`--system-site-packages` 复用 torch，只多装了 demucs + einops + julius |

ffmpeg 不在 PATH，实际在：

```
H:\SHARE\ComfyUI\miniconda3\envs\comfyui\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe
```

`doctor` 和 `make_clip` 会自动从 `imageio_ffmpeg` 找到它，不用配 PATH。

### 自检

```powershell
cd C:\dub\dub-engine
C:\dub\venv\Scripts\python.exe -m dub doctor
```

期望看到：

```
GPU         : AMD Radeon(TM) 8060S Graphics / 78.2 GB / ROCm 7.2.0
  ok torch            2.10.0+rocm7.12.0a20260311
  ok demucs           ...
```

### 跑一段（先用 stub 验证管线）

```powershell
# 从已下载的 B 站音轨切 120 秒
C:\dub\venv\Scripts\python.exe tools\make_clip.py `
  --in data\upos-sz-mirrorhw.bilivideo.com_41835920.m4s `
  --out data\clip120.wav --start 60 --seconds 120

# 全链路（stub 识别/翻译/合成，真分离）
C:\dub\venv\Scripts\python.exe -m dub run `
  --input data\clip120.wav `
  --asr stub --mt stub --tts stub `
  --out out\test_sep.wav
```

实测（2026-10-30）：

| 阶段 | 耗时 |
|---|---|
| extract | 0.2 s |
| **separate（Demucs, ROCm）** | **58.5 s** |
| asr（stub） | 1.4 s |
| tts（stub） | 0.1 s |
| assemble | 0.4 s |

分离是当前瓶颈，**约 2× 实时**（120 秒音频 58 秒）。10 分钟视频约 5 分钟分离。

> 跑的时候会刷一屏 `MIOpen(HIP): Error ... gfx908_metadata.tn.model`。这是 ROCm 缺预编译启发式文件，不影响结果。嫌吵就设 `MIOPEN_LOG_LEVEL=3`。

### 接真模型

**ASR**：AMD 上 `faster-whisper`（CTranslate2）只能走 CPU，别指望 GPU。走 `transformers` 后端：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"   # 国内加速，可选
C:\dub\venv\Scripts\python.exe -m dub run `
  --input data\clip120.wav --asr transformers `
  --mt stub --tts stub --out out\test_asr.wav
```

首次会下 `openai/whisper-large-v3-turbo`（约 1.6 GB）。想省显存可以改 `stages.py` 里的 `model_id` 为 `openai/whisper-small`。

**翻译**：任何 OpenAI 兼容接口都能接。

```powershell
$env:DUB_LLM_BASE  = "https://api.deepseek.com/v1"
$env:DUB_LLM_KEY   = "sk-..."
$env:DUB_LLM_MODEL = "deepseek-chat"

C:\dub\venv\Scripts\python.exe -m dub run `
  --input data\clip120.wav --asr transformers --mt llm --tts edge `
  --lang zh --out out\test_zh.wav
```

翻译会按 `--chars-per-sec 4.5` 给每行算字数预算，写进 prompt 里逼模型压缩。

**TTS**：
- `--tts edge`：零门槛，`edge-tts` 已装，用微软在线音色（`zh-CN-XiaoxiaoNeural` 等），**不做音色克隆**，适合先跑通。
- `--tts cosyvoice`：本地克隆，需要 `DUB_COSYVOICE_DIR`、`DUB_COSYVOICE_MODEL` 和 `--ref-wav`，模型还没装。

---

## 管线细节

### 缓存

工作目录 `work/<hash>/`，hash 由「输入路径 + 大小 + mtime + 语言 + 各后端 + 音色」算出。每一步产物落盘：

```
work/<hash>/
  mix.wav       抽取后的 44.1k 立体声
  vocals.wav    分离出的人声
  bed.wav       去掉人声的伴奏
  asr.json      识别结果
  trans.json    翻译结果
  tts/seg_*.wav 逐句合成
  final.wav     混音成品
  summary.json  各阶段耗时
```

改一个参数会生成新目录；只想重跑某步用 `--force trans`（逗号分隔：`mix,bed,asr,trans,tts,final`）。

### 混音逻辑

- 用 `bed.wav`（去人声）当底，不用原始混音——这样即使分离有残留也不会把原声放两遍。
- 说话段对 bed 做 **-8 dB 闪避**（`--duck-db`），120 ms 起音 / 250 ms 释放。
- 配音若比原句长 5% 以上，用 librosa 的 time-stretch 压回去，最多 1.25×（`max_stretch`）。
- 成品峰值归一化到 -1 dBFS。

### 失败兜底

分离失败（模型没装、显存不够）会自动退回 **duck 模式**：不分离，直接把原混音压低叠配音。`summary.json` 里 `"separated": false` 会标出来。

---

## 已知问题

1. **`torchaudio.load` 在 2.10 要 torchcodec**，所以分离阶段绕开 torchaudio，改走 soundfile。别改回去。
2. **MIOpen 报错刷屏**：ROCm 缺 gfx908 启发式文件，纯噪音。`MIOPEN_LOG_LEVEL=3` 可压。
3. **`faster-whisper` 在 AMD 上没 GPU 加速**，只能 CPU；GPU 走 `transformers`。
4. **分离是瓶颈**：2× 实时。想快可以换 `htdemucs_ft`（更慢更好）或降 `segment`，或接受它。
5. **还没做说话人分离（diarization）**，所有台词用同一个音色。这是下一步。

---

## 下一步（按优先级）

1. **跑通真 ASR**：`--asr transformers` 测 120 秒，记录耗时和显存。这一步决定 Whisper 在 8060S 上能不能用。
2. **接 TTS**：先 `edge` 验证全链路听感，再决定装 CosyVoice 3 还是 IndexTTS-2.5。
3. **加 diarization**：`pyannote` 或 `3D-Speaker`，把角色分开，每个角色一个参考音色。
4. **音色距离约束**：见 v2 架构文档第 2.4 节，解决"两个角色声音太像"。
5. **回灌到浏览器**：把成品音轨按时间轴喂给 `<video>`，做"绿灯"交互。

## 相关文档

- 可行性研究：`Obsidian Vault/技术笔记/网页视频实时换语言-AI配音可行性研究-2026-10.md`
- 架构设计 v2：`Obsidian Vault/技术笔记/网页视频AI配音-架构设计-v2-2026-10.md`
