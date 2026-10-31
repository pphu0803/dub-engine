# dub-engine — 网页视频 AI 实时配音

> 打开视频网站，一键把当前视频的语言转成中文配音——无需下载，无需剪辑，浏览器里直接看。
> **Watch any web video with AI dubbing in your language — no downloads, no editing, right in the browser.**

在 B 站视频页点一下「🎙 AI 配音」，约两分钟后配音开始播放：**原视频的背景音乐和音效原样保留，只有台词被换成了中文**，并与画面节奏对齐。处理在你的电脑上分段流式进行——第一分钟先出声，后面边看边出，处理速度始终快于播放速度。

## 它能做什么

1. **捕获音轨**：插件自动找到当前视频的音轨地址（不下载整个视频）
2. **本地流水线**：人声分离（Demucs）→ 语音识别（Whisper）→ LLM 翻译 → AI 配音 → 句子级对齐混音，全程在你的 GPU 上分段进行
3. **流式回传**：第一段完成即开始播放；处理速度（每分钟片段约 45 秒处理完）持续快于播放速度
4. **随时切回**：原声 / 配音一键切换；进度条随便拖

## 特性

- **准实时**：分段流式架构（~60 秒/段），无需等待整片处理完
- **节奏对齐**：句子级切分，每句起点对齐原句、语速自动贴合到下一句起点，台词跟着表演和口型走
- **多说话人**：说话人自动分组（声纹聚类），不同说话人分配性别/年龄匹配的不同音色
- **背景保留**：人声分离只换台词，背景音乐、环境音、音效原样保留
- **本地优先**：音视频全部本地处理；只有台词文本发送给你自己配置的 LLM
- **断点缓存**：同一视频重复配音秒级返回；单段失败不影响其余段落，一键续跑
- **兼容浏览器**：Chrome / Edge（MV3 插件）

## 当前限制

- **站点**：目前支持 B 站（DASH 音轨直链）；YouTube 部分可用，其他站点待适配
- **目标语言**：目前仅支持**输出中文**（架构已支持任意语言，配置开放中）
- **硬件**：需要本机 GPU（实测 AMD ROCm 可用，应兼容 NVIDIA；纯 CPU 速度不可用）
- **首次运行**：需下载模型（Whisper ~1.6 GB、Demucs ~80 MB、声纹模型 ~28 MB，自动进行）
- 翻译质量取决于你配置的 LLM；连续强 BGM 场景下人声分离会有残留

---

## 环境要求

| 依赖 | 要求 | 说明 |
|---|---|---|
| Python | 3.10 – 3.12 | 开发环境为 3.12 |
| PyTorch | 与你的 GPU 匹配的版本 | NVIDIA 用 CUDA 版，AMD 用 ROCm 版；见下方安装步骤 |
| GPU | NVIDIA / AMD 独显或核显 | 人声分离与语音识别跑在 GPU 上；纯 CPU 可运行但速度不可用 |
| FFmpeg | **无需安装** | 引擎自动使用 pip 包自带的二进制（imageio-ffmpeg） |
| LLM API Key | 任意 OpenAI 兼容接口 | DeepSeek、通义、Kimi、OpenAI 等均可 |
| 浏览器 | Chrome 或 Edge | 加载 MV3 插件 |

系统依赖：无额外要求，全部依赖走 pip 安装。

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/pphu0803/dub-engine.git
cd dub-engine
```

### 2. （推荐）创建虚拟环境

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 3. 安装 PyTorch（按你的 GPU 平台选择其一）

```bash
# NVIDIA（CUDA 12.x）
pip install torch --index-url https://download.pytorch.org/whl/cu121

# AMD（ROCm 6.x，Linux；Windows 上的 AMD 需要 ROCm 7.2+ 的预览版 wheel）
pip install torch --index-url https://download.pytorch.org/whl/rocm6.2

# 仅 CPU（可跑通，但速度不适合看视频）
pip install torch
```

### 4. 安装其余依赖

```bash
pip install -r requirements.txt
```

### 5. 模型下载（首次运行自动进行，也可提前挂好网络）

| 模型 | 大小 | 来源 |
|---|---|---|
| Whisper large-v3-turbo（语音识别） | ~1.6 GB | HuggingFace |
| Demucs htdemucs（人声分离） | ~80 MB | 自动 |
| CAM++（说话人声纹） | ~28 MB | ModelScope |

> 国内网络建议先设置 HuggingFace 镜像再启动：
> ```bash
> # Windows (PowerShell)
> $env:HF_ENDPOINT = "https://hf-mirror.com"
> # Linux / macOS
> export HF_ENDPOINT=https://hf-mirror.com
> ```

## 配置 LLM（三选一）

引擎需要一个 OpenAI 兼容的 LLM 来翻译台词。**优先级：启动参数 > 环境变量 > config.json**。

### 方式 A — 配置文件（推荐）

```bash
cp config.example.json config.json
```

编辑 `config.json`（此文件已被 gitignore，不会被提交）：

```json
{
  "llm": {
    "base": "https://api.deepseek.com/v1",
    "key": "sk-你的key",
    "model": "deepseek-chat"
  }
}
```

### 方式 B — 环境变量

```bash
# Windows (PowerShell)
$env:DUB_LLM_BASE = "https://api.deepseek.com/v1"
$env:DUB_LLM_KEY  = "sk-你的key"
$env:DUB_LLM_MODEL = "deepseek-chat"

# Linux / macOS
export DUB_LLM_BASE="https://api.deepseek.com/v1"
export DUB_LLM_KEY="sk-你的key"
export DUB_LLM_MODEL="deepseek-chat"
```

### 方式 C — 启动参数

见下一节的 `--llm-*` 参数。

## 启动本地服务

```bash
python -m dub serve --port 8765
```

带 LLM 参数的完整示例：

```bash
python -m dub serve --port 8765 \
  --llm-base "https://api.deepseek.com/v1" \
  --llm-key "sk-..." \
  --llm-model "deepseek-chat"
```

启动成功的标志（引擎控制台）：

```
[serve] LLM: deepseek-chat @ https://api.deepseek.com/v1
[serve] http://127.0.0.1:8765  (Ctrl+C 退出)
```

**验证**：浏览器打开 `http://127.0.0.1:8765/api/health`，看到 `{"ok": true}` 即服务正常。

说明：

- 服务**只绑定 127.0.0.1**，不对外网开放，无需担心防火墙弹窗
- 控制台窗口保持开启即持续服务；关闭即停止（配音结果已落盘，重启后同一视频秒级返回）
- 首次处理视频时，控制台会依次打印模型下载进度与各阶段耗时
- 如需更换端口：`--port 8888`，并同步修改 `extension/content.js` 顶部的 `SERVER` 常量

## 安装浏览器插件

1. 打开 `chrome://extensions`（Edge 为 `edge://extensions`）
2. 开启「开发者模式」→「加载解压缩的扩展程序」
3. 选择本仓库的 `extension/` 目录
4. （可选）把插件固定到工具栏

## 使用方法

### 配音一个视频

1. 打开一个 B 站视频页，**先播放 2 秒**——播放器会把音轨地址暴露出来，插件靠这个抓轨
2. 点页面右上角「🎙 AI 配音」
3. 状态胶囊依次显示：`读页面记录 → 排队 → 抽取整轨 → 配音中 N/M 段`
   （每段内部走：Demucs 分离 → Whisper 识别 → LLM 翻译 → TTS 合成 → 对齐混音）
4. **第一段完成即自动开始播放**（首次约 2–4 分钟，含模型下载），之后边看边出
5. 原视频自动静音，背景音乐保留在配音轨里

### 三个按钮

| 按钮 | 作用 |
|---|---|
| 🎙 AI 配音 | 捕获音轨并开始流水线 |
| 🔈 切回原声 / 🔊 切回配音 | 原声与配音一键互换，进度不丢 |
| 🎙 重新配音 | 失败段续跑；对齐逻辑升级后会生成全新版本 |

### 拖进度条

随便拖。拖到**尚未处理**的段落时自动回退原声（不静音），该段在后台就绪后自动切回配音。

### 缓存与续跑

- 同一视频再次点击：秒级返回已完成的配音（按音轨内容哈希缓存，与 URL 无关）
- 关页面、关引擎都不丢结果——重新点击自动从断点续跑
- 单段失败（如网络抖动）不影响其余段落：会提示「N/M 段完成」，点「重新配音」只补失败段

### 常见问题（Troubleshooting）

| 现象 | 处理 |
|---|---|
| ❌ 连不上本地引擎 | 引擎没启动或端口不对；先访问 `http://127.0.0.1:8765/api/health` 验证 |
| ❌ 没找到像音轨的请求 | 刷新视频页，先播放 2 秒再点按钮（点按钮时视频在播最稳） |
| ❌ 段 N: ...timeout / ConnectionError | 网络抖动——点「重新配音」只续跑失败段；LLM 调用已内置直连+4 次重试 |
| 首次点击很慢 | 正在下载 Whisper 模型（1.6 GB），进度看引擎控制台；国内先设 `HF_ENDPOINT` 镜像 |
| 所有台词同一个音色 | 声纹模型自动下载失败（看引擎日志）；重新点击会重试下载 |
| 报 403 / 插件加载失败 | 确认 extension 目录完整（含 manifest.json）；用「重新加载」而非重复安装 |

## 工作原理

```
浏览器插件 ──音轨URL──▶ 本地引擎（127.0.0.1:8765）
                          │
                          ▼
        按静音点切 ~60s 段，段间流水线（段0优先）：
        Demucs 人声分离 → VAD → Whisper 识别 → 说话人声纹聚类
        → LLM 翻译（按窗口字数预算压缩）→ F0 画像匹配音色 TTS
        → 句子级对齐混音（背景保留，逐句贴合原句节奏）
                          │
插件 ◀──段级 mp3──────── 段 0 完成即回传，边播边出
```

- **对齐**：每句配音的起点对齐原句起点，语速自动贴合到下一句起点——台词跟着表演走
- **说话人**：声纹嵌入 + 层次聚类，跨段编号一致；每个说话人按基频/音色匹配中文配音声线（性别与年龄段对得上）
- **幻觉过滤**：VAD 切区 + 文本循环检测双重保险，音乐段不会产生"谢谢观看"式幽灵台词
- **缓存**：任务按音轨内容哈希 + 目标语言命名，重复请求秒级返回；中断续跑

## 项目结构

```
dub/            引擎：流水线、分段调度、说话人、声学画像、HTTP 服务
extension/      浏览器插件（MV3，纯内容脚本，无后台进程、零权限）
tools/          音轨探测等辅助脚本
config.example.json   LLM 配置模板（复制为 config.json 使用）
```

## Roadmap

- [ ] 更多站点：YouTube 完整支持、通用 HLS/m3u8、通用网页自动适配
- [ ] 多目标语言（英/日/韩输出）
- [ ] 音色克隆：让中文配音保留原说话人的音色
- [ ] 口型级对齐（音素级时间戳）
- [ ] 安装器打包 / Firefox 支持

## 致谢

- [Demucs](https://github.com/adefossez/demucs)（Meta, MIT）— 音乐/人声分离
- [Whisper](https://github.com/openai/whisper)（OpenAI, MIT）— 语音识别
- [silero-vad](https://github.com/snakers4/silero-vad)（MIT）— 语音活动检测
- [3D-Speaker / CAM++](https://github.com/modelscope/3D-Speaker)（Apache-2.0）— 说话人嵌入
- [edge-tts](https://github.com/rany2/edge-tts)（GPL-3.0）— 语音合成
- [Librosa](https://github.com/librosa/librosa)（ISC）— 音频处理

## License

[MIT](LICENSE)
