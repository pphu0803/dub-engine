# dub-engine

网页视频 AI 配音引擎。当前进度：**音轨预取已验证 → 离线管线骨架跑通 → Demucs 在 ROCm 上跑通 → 真 ASR（whisper-large-v3-turbo）跑通**。

## 现在能做什么

```
取音轨 → 分离人声/伴奏 → 识别 → 翻译 → 合成 → 对齐混音 → 成品 wav
  ✅        ✅ Demucs     ✅ GPU    ⏳      ⏳        ✅
```

- ✅ 已跑通：音轨预取（B 站实测 54 MB / 4 s）、解码、Demucs 分离（ROCm，2× 实时）、ducking 混音、整条管线的缓存与编排、真 ASR（GPU 2.7× 实时，峰值显存 2.2 GB）。
- ⏳ 待接：翻译（LLM 接口已写好，缺 key）、TTS（CosyVoice 适配器已写，未装模型）。
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
  hf_fastdl.py          HF 模型并行下载（LFS 大文件走 ModelScope，见下文）
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
| **装 demucs 的环境** | `C:\code\dub\venv\Scripts\python.exe` | 从上面那个 venv 出来，`--system-site-packages` 复用 torch，只多装了 demucs + einops + julius |

ffmpeg 不在 PATH，实际在：

```
H:\SHARE\ComfyUI\miniconda3\envs\comfyui\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe
```

`doctor` 和 `make_clip` 会自动从 `imageio_ffmpeg` 找到它，不用配 PATH。

### 自检

```powershell
cd C:\code\dub\dub-engine
C:\code\dub\venv\Scripts\python.exe -m dub doctor
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
C:\code\dub\venv\Scripts\python.exe tools\make_clip.py `
  --in data\upos-sz-mirrorhw.bilivideo.com_41835920.m4s `
  --out data\clip120.wav --start 60 --seconds 120

# 全链路（stub 识别/翻译/合成，真分离）
C:\code\dub\venv\Scripts\python.exe -m dub run `
  --input data\clip120.wav `
  --asr stub --mt stub --tts stub `
  --out out\test_sep.wav
```

实测（2026-10-30，120 s B 站植物纪录片片段）：

| 阶段 | 耗时 | 备注 |
|---|---|---|
| extract | 0.4 s | |
| **separate（Demucs, ROCm）** | **21–25 s** | 首跑 58 s 含模型下载与 MIOpen 预热；热缓存后 ~21 s（~5× 实时） |
| **asr（whisper-large-v3-turbo, fp16 GPU）** | **45–57 s** | 纯推理 45 s（**2.7× 实时**），管线内首跑 57 s 含模型加载 5 s；峰值显存 **2.16 GB**（8060S 可见 84 GB，余量巨大） |
| tts（stub） | 0.1 s | |
| assemble | 0.3 s | |

分离热跑约 5× 实时、识别 2.7× 实时——离线配音全链路 GPU 部分合计约 1.6× 实时，10 分钟视频约 6 分钟出活。

> 跑的时候会刷一屏 `MIOpen(HIP): Error ... gfx908_metadata.tn.model`。这是 ROCm 缺预编译启发式文件，不影响结果。嫌吵就设 `MIOPEN_LOG_LEVEL=3`。

### 接真模型

**ASR**：AMD 上 `faster-whisper`（CTranslate2）只能走 CPU，别指望 GPU。走 `transformers` 后端：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"   # 国内加速，可选
C:\code\dub\venv\Scripts\python.exe -m dub run `
  --input data\clip120.wav --asr transformers `
  --mt stub --tts stub --out out\test_asr.wav
```

首次会下 `openai/whisper-large-v3-turbo`（约 1.6 GB）。**别用多连接并行 Range 下载 HF 大文件**——hf-mirror 对 LFS 直接 302 到 huggingface.co 的 xet CDN，实测并行分片两轮全量下载 sha256 都不匹配（坏字节），单流又只有 ~230KB/s。可靠姿势（实测 5.2MB/s，字节与 HF 逐位一致）：

```powershell
# ModelScope 国内镜像单流下载 → 校验 → 种进 HF 缓存
curl -sL -C - -o model.safetensors --retry 8 `
  "https://modelscope.cn/models/AI-ModelScope/whisper-large-v3-turbo/resolve/master/model.safetensors"
# sha256 应为 542566a422ae4f3fd23f1ba11add198fca01bbf82e66e6a2857b3f608b1eb9d1
# 然后放入 %USERPROFILE%\.cache\huggingface\hub\models--openai--whisper-large-v3-turbo\
#   blobs\<sha256> 与 snapshots\<rev>\model.safetensors（本机拷贝模式，非符号链接）
```

仓库里的 `tools\hf_fastdl.py` 是并行下载器（含分片断点续传与 X-Linked-ETag 校验参照），适合小文件/非 LFS；LFS 大文件请走上面的 ModelScope 路子。

想省显存可以改 `stages.py` 里的 `model_id` 为 `openai/whisper-small`。

**翻译**：任何 OpenAI 兼容接口都能接。

```powershell
$env:DUB_LLM_BASE  = "https://api.deepseek.com/v1"
$env:DUB_LLM_KEY   = "sk-..."
$env:DUB_LLM_MODEL = "deepseek-chat"

C:\code\dub\venv\Scripts\python.exe -m dub run `
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
4. **transformers 后端的 ASR 没有 VAD**：安静段/纯音乐段会出现幻觉循环（实测 120s 片段里"森林肯定的"重复三遍）；两次运行分段数还会漂（19 → 12 → 13 段）。faster-whisper 的 `vad_filter=True` 没法直接搬，要 VAD 得自己接（silero-vad 之类）或等接 diarization 时一起做。
5. **还没做说话人分离（diarization）**，所有台词用同一个音色。这是下一步。
6. ~~缓存判断 bug~~（已修 2026-10-30）：`_need("mix")` 查的是 `mix` 不是 `mix.wav`，导致缓存永远 miss、每次全量重跑。现已用 `CACHE_FILES` 短名映射表修正。

---

## 下一步（按优先级）

1. ~~**跑通真 ASR**~~：✅ 2026-10-30 完成。whisper-large-v3-turbo 在 8060S 上 2.7× 实时、峰值显存 2.16 GB——完全可用，还剩 80 GB 显存余量给 TTS/diarization 同驻。
2. **接 TTS**：先 `edge` 验证全链路听感，再决定装 CosyVoice 3 还是 IndexTTS-2.5。
3. **加 VAD / 幻觉抑制**：见已知问题 4；可与 diarization 合并做。
4. **加 diarization**：`pyannote` 或 `3D-Speaker`，把角色分开，每个角色一个参考音色。
5. **音色距离约束**：见 v2 架构文档第 2.4 节，解决"两个角色声音太像"。
6. **回灌到浏览器**：把成品音轨按时间轴喂给 `<video>`，做"绿灯"交互。

## 相关文档

- 可行性研究：`Obsidian Vault/技术笔记/网页视频实时换语言-AI配音可行性研究-2026-10.md`
- 架构设计 v2：`Obsidian Vault/技术笔记/网页视频AI配音-架构设计-v2-2026-10.md`
