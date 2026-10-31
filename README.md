# dub-engine — 网页视频 AI 实时配音

> 打开视频网站，一键把当前视频的语言转成中文配音——无需下载，无需剪辑，浏览器里直接看。
> **Watch any web video with AI dubbing in your language — no downloads, no editing, right in the browser.**

在 B 站视频页点一下「🎙 AI 配音」，约两分钟后配音开始播放：**原视频的背景音乐和音效原样保留，只有台词被换成了中文**，并与画面节奏对齐。处理在你的电脑上分段流式进行——第一分钟先出声，后面边看边出，处理速度始终快于播放速度。

## 它能做什么

1. **捕获音轨**：插件自动找到当前视频的音轨地址（不下载整个视频）
2. **本地流水线**：人声分离（Demucs）→ 语音识别（Whisper）→ LLM 翻译 → AI 配音 → 句子级对齐混音，全程在你的 GPU 上分段进行
3. **流式回传**：第一段完成即开始播放；处理速度（每分钟片段约 45 秒处理）持续快于播放速度
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
- **首次运行**：需下载模型（Whisper ~1.6 GB、Demucs ~80 MB，自动进行）
- 翻译质量取决于你配置的 LLM；连续强 BGM 场景下人声分离会有残留

## 快速开始

### 0. 准备

- Python 3.10+ 与 Git
- 本机 GPU（AMD ROCm 或 NVIDIA CUDA 版 PyTorch，按你的平台安装）
- 一个 **OpenAI 兼容**的 LLM API Key（DeepSeek、通义、Kimi、OpenAI 等均可）

### 1. 安装

```bash
git clone https://github.com/pphu0803/dub-engine.git
cd dub-engine
pip install -r requirements.txt
# torch 按你的 GPU 平台自行安装，例如 NVIDIA：
# pip install torch --index-url https://download.pytorch.org/whl/cu121
```

### 2. 配置 LLM（三选一）

**方式 A — 配置文件（推荐）**：

```bash
cp config.example.json config.json
# 编辑 config.json，填入你的 base / key / model
```

**方式 B — 环境变量**：

```bash
export DUB_LLM_BASE="https://api.deepseek.com/v1"
export DUB_LLM_KEY="sk-..."
export DUB_LLM_MODEL="deepseek-chat"
```

**方式 C — 启动参数**：

```bash
python -m dub serve --llm-base "https://api.deepseek.com/v1" --llm-key "sk-..." --llm-model "deepseek-chat"
```

### 3. 启动引擎

```bash
python -m dub serve --port 8765
```

### 4. 安装浏览器插件

1. 打开 `chrome://extensions`（Edge 为 `edge://extensions`）
2. 开启「开发者模式」→「加载解压缩的扩展程序」
3. 选择本仓库的 `extension/` 目录

### 5. 使用

1. 打开一个 B 站视频，**播放 2 秒**（让音轨地址出现）
2. 点右上角「🎙 AI 配音」
3. 约两分钟后第一段配音开始播放，之后边看边出
4. 右上角可随时**切回原声 / 切回配音**，进度条随便拖

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
