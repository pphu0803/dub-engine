# dub-engine

网页视频 AI 配音引擎。当前进度：**v4（2026-10-30 深夜）**——全链路 + VAD + 说话人分离 + **声学画像性别/年龄匹配音色**，默认目标语言中文。

## 现在能做什么

```
取音轨 → 分离 → VAD切区 → 识别+幻觉过滤+diarize → 画像(性别/年龄) → LLM翻译 → 匹配音色TTS → 混音
  ✅      ✅      ✅            ✅                    ✅              ✅        ✅        ✅
```

- ✅ v4a 实测（120 s 中英混说 → **中文**）：`out/first_v4.wav`，男解说（F0 92Hz）→ 云扬沉稳男声，女嘉宾（F0 222Hz）→ 晓晓女声，7 段 overflow 0。
- **F0 画像是硬约束**：v3 曾把 222Hz 的女嘉宾配成 Guy 男声——"老年男配年轻女"这类错配靠它杜绝（`dub/profile.py`，pyin 基频 + 频谱质心，edge 合成音频闭环验证）。
- 🧪 全链路可以用 `--asr stub --mt stub --tts stub` 跑通，验证时间轴和混音。

## 目录

```
dub/
  pipeline.py   编排 + 缓存（按输入哈希分目录，哪步坏了只重跑那步）
  stages.py     extract / separate / asr+VAD+幻觉过滤 / translate / tts / assemble
  diarize.py    CAM++ 嵌入 + 层次聚类 → speaker 标签
  campplus.py   CAM++ 网络定义（vendor 自 3D-Speaker，Apache-2.0）
  audio.py      读写、重采样、time-stretch、ducking 包络
  doctor.py     环境自检
  cli.py        命令行
models/
  campplus_cn_common.bin   CAM++ 权重（ModelScope，不入库）
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
| translate（deepseek-flash） | 9.3 s | 11 段一次 API 调用；译文按窗口字数预算压缩，质量自然 |
| tts（edge, Aria） | 22.3 s | 11 段串行 WebSocket |
| assemble | 1.4 s | |

分离热跑约 5× 实时、识别 2.7× 实时——离线配音全链路 GPU 部分合计约 1.6× 实时，10 分钟视频约 6 分钟出活。

### 第一版复现命令（中→英示例）

```powershell
$env:DUB_LLM_BASE = "https://api.deepseek.com/v1"    # 任何 OpenAI 兼容接口都行
$env:DUB_LLM_KEY  = "sk-..."
$env:DUB_LLM_MODEL = "deepseek-flash"
C:\code\dub\venv\Scripts\python.exe -m dub run `
  --input data\clip120.wav --asr transformers --mt llm --tts edge `
  --lang en --chars-per-sec 12 `
  --out out\first_v3.wav
```

- **不带 `--voice` = 多音色模式**：diarize 的说话人序号索引 `EDGE_SPEAKER_VOICES[lang]` 音色池；显式给 `--voice` 则全程单音色。**注意 `--voice` 参与缓存 key**：带不带会落到不同 work 目录。
- **`--chars-per-sec` 按目标语言调**：中文 4.5（默认），英文 12——预算直接进翻译 prompt，调对了 overflow 才低。
- `--force` 少写哪步哪步不重做（如上游 asr 变了要 `--force trans,tts,final`）。

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
4. **ASR 稳定性三连**：① 配乐吟唱段的幻觉循环已由 VAD 切区 + 文本过滤双保险治理（VAD 本身救不了哼唱——silero 判其为人声，能量也不低，只能靠文本信号）；② **whisper 中文繁简摇摆**——同一段音频三次运行三种繁简度（"极端恶劣"→"極端惡劣"），想稳定给 `--source-lang zh`；③ VAD 边缘区摇摆（语音总长 73.9~76.9s 漂），分段数随之漂。全轨长音频模式在混语料上会**把英文解说转成中文循环幻觉**——VAD 逐区转写后该段转出流利英文（这就是 v1"误杀 23s 真解说"的真相）。
5. **edge-tts 必须 ≥7.3.1**：conda 里自带的 7.2.8 连微软 WebSocket 直接 403（Sec-MS-GEC 校验变了）。已在 venv 内升级遮蔽，不动 conda 基座。**偶发瞬时 403 已加重试（3 次退避）**。
6. **conda 的 modelscope 包是残缺的**（缺 addict/datasets 一串依赖，装齐要拉全家桶）——别用。CAM++ 走 vendor 路线：`dub/campplus.py`（网络定义）+ `models/campplus_cn_common.bin`（28MB，ModelScope 直下 13MB/s）。前处理 = torchaudio kaldi fbank + 句级均值归一（**fbank 输入要 2D**，1D 会 IndexError）；`CAMPPlus(embedding_size=192)`（默认 512 会对不上权重）。正确性用官方 example 验过：同说话人 cos 0.69 / 异 -0.08。
7. **CosyVoice2 音色克隆：已验收环境、产出乱语，判 parked（2026-10-31）**。全过程：
   - **已打通**：仓库+Matcha 子模块+3.8GB 权重（ModelScope 13MB/s）；8 个依赖坑（addict/HyperPyYAML/lightning/pkg_resources<81/gdown/wget/pyarrow/pyworld/openai-whisper）；torchaudio 2.10 补丁（`load_wav` 改 soundfile，v2.0 与 master 签名不同各打一份）；LLM 权重是 bf16 而 ROCm matmul 报 dtype 错 → 三件套 `.float()`；v2.0 API 传 16k tensor、master 传路径。
   - **症状**：合成跑通但输出乱语——时长撞 max_len 天花板（40 字出 32.8s）、女声参考克隆出 126Hz 男声、whisper 回读为空。
   - **已排除**：ROCm 数值（CPU 推理结果一致）、权重错位（strict=True）、文本 token（与 BlankEN tokenizer 逐 id 一致）、repo 版本（master 与 v2.0 结果一致）、mel padding（见下）。
   - **S1/S2 实验（2026-10-31）**：S1 实锤 speech tokenizer 对 mel padding 敏感——不 pad 348 帧→87 token，pad 到 30s（whisper-20231117 默认行为，官方 pin 的版本）→3000 帧→750 token，前缀 39/87 不同。但 S2 恢复 padding 后仍乱生成 → **假说被否决**，tokenizer 不是病根。
   - **剩余嫌疑**：torch 2.10（pin 2.3.1）/ transformers 5.x（pin 4.51.3）的 Qwen2 骨干行为差异。**修复要降级共享的 ComfyUI conda 环境——不做**。
   - **复产路径**：独立环境（单独 venv pin torch==2.3.1+cu121），最佳宿主是 3060 机（原生 CUDA）。权重留在 `C:\code\dub\models\cosyvoice2-0.5B`、补丁留在 `C:\code\dub\CosyVoice`（v2.0 tag，工作树有 load_wav 补丁未提交）。
   - **当前方案**：说话人音色用 F0 画像 → edge 音色池匹配（`EDGE_VOICE_BY_PROFILE`），性别/年龄保真已满足验收；克隆质量"不要求保真"，边际收益有限。
8. ~~缓存判断 bug~~（已修 2026-10-30）：`_need("mix")` 查的是 `mix` 不是 `mix.wav`，导致缓存永远 miss、每次全量重跑。现已用 `CACHE_FILES` 短名映射表修正。另注意 `--force` 不含 `final` 时，上游变了混音不会重做。

---

## 下一步（按优先级）

1. ~~跑通真 ASR~~：✅ 2026-10-30。
2. ~~全链路第一版~~：✅ 2026-10-30。
3. ~~diarization + VAD~~：✅ 2026-10-30。
4. ~~默认中文 + 性别/年龄匹配音色~~：✅ 2026-10-30（v4a，F0 画像硬约束）。
5. **音色克隆**：parked，复产路径见已知问题 7（独立 pinned 环境，3060 机优先）。
6. **音色距离约束**：见 v2 架构文档第 2.4 节。
7. **回灌到浏览器**：把成品音轨按时间轴喂给 `<video>`，做"绿灯"交互。

## 浏览器插件（v0.1）

```
插件 (MV3)                                本地引擎
  content.js  注入按钮/状态pill/同步播放     POST /api/dub {url, referer} 或 {path}
      ⇅ chrome.runtime                     GET  /api/job/<id>       阶段/时间轴
  background.js  webRequest 抓音轨URL  ⇄   GET  /api/job/<id>/audio 成品mp3
  （5s 窗口，audio 命中词优先）              （127.0.0.1:8765，GPU 串行）
```

**启动**：

```powershell
# 1. 起引擎（需要 LLM 环境变量，同 run）
C:\code\dub\venv\Scripts\python.exe -m dub serve --port 8765
# 2. 浏览器（Edge/Chrome）→ 扩展管理 → 开发人员模式 → 加载解压缩扩展
#    选 C:\code\dub\dub-engine\extension\
# 3. 打开视频页 → 播放 2 秒 → 点右上角「🎙 AI 配音」→ 等进度 → 自动切换
```

**同步播放**：原声静音，Audio 元素播配音母版（bed+配音），play/pause/seek/倍速全跟随，漂移 >0.35s 自动校正。音频经 service worker fetch → blob URL 回传页面，绕开 https→http 混合内容限制；服务端所有响应带 CORS + PNA 头。

**v1 限制**：只支持直链音轨（B 站 DASH audio m4s ✓，HLS/m3u8 未接）；音轨捕获靠点按钮后 5 秒窗口内视频正在播放；一次只跑一个 job。
