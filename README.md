# dub-engine

网页视频 AI 配音引擎。第一阶段只做一件事：**验证音轨能不能被"提前、快速"拿到**——这是整个预取架构的地基。

## 现在有什么

```
tools/
├── probe_media.py        # 测音轨获取速度（HLS / DASH / 直链）
├── decode_audio.py       # 媒体 → 16k/24k 单声道 wav（不依赖系统 ffmpeg）
└── browser/collect.js    # 在视频页面里找出音轨地址（粘贴到 DevTools Console）
```

## 快速开始

### 1. 在视频页面找音轨地址

打开目标视频页 → F12 → Console → 整段粘贴 `tools/browser/collect.js` → 回车。

它会列出页面已经发出的媒体请求，逐个取前 1 MB 测速，最后打印一条建议地址。

### 2. 测这条地址能不能"几秒拿完"

```powershell
python tools/probe_media.py --url "<上一步的地址>" --referer "<视频页地址>"
```

有些站点要登录态，再加 `--cookie "SESSDATA=..."`。

### 3. 看结论

```
倍率        : 106.6× 实时
10 分钟音频 : 约 5.6 s
解码检查    : codec=aac rate=48000 ch=2 dur=339.7s
```

`倍率` 是"每 1 秒真实时间能拿多少秒媒体"。**只要 > 10×，预取架构就成立**；< 2× 就只能退回实时捕获。

### 4. 转成 ASR 能吃的 PCM

```powershell
python tools/decode_audio.py --in data\xxx.m4s
```

默认输出 16 kHz 单声道（ASR 用），`--rate 24000` 给 TTS 做参考音。会报 RMS、峰值和帧级静音占比——下到静音流时能立刻发现。

## 已实测（2026-10-30）

| 源 | 类型 | 选中 | 码率 | 倍率 | 10 min 耗时 |
|---|---|---|---|---|---|
| test-streams.mux.dev | HLS | 最低码率变体（含视频） | — | 128× | 4.7 s |
| Apple bipbop fmp4 | HLS + BYTERANGE | 音频轨 English 2ch | 89 kbps | 107× | 5.6 s |
| Unified Streaming | DASH | audio AdaptationSet | ~66 kbps | 30× | 20 s |

结论：**音轨本身的体积很小（10 分钟 60–90 kbps 约 5–10 MB），瓶颈从来不是带宽，是能不能拿到地址。**

## 踩过的坑

**`EXT-X-BYTERANGE`**：Apple 这类单文件切片把 101 个分片指向同一个 `main.mp4`，只靠 URL 去下会重复拉整个文件（实测 109 MB vs 正确 6.7 MB）。现在按 Range 请求，拼出来的文件能正常解码。

**限速按批提交**：8 个并发一起发出去，上限会超出约一个批次（6 MB 限制实测落到 6.7 MB）。可以接受，但报告里要说明。

**DASH 的覆盖**：目前支持 `SegmentTemplate`（`$Number$` / `SegmentTimeline`）和 `SegmentList`；`SegmentBase` + `indexRange` 的单文件写法还没做，遇到会报"未解析出分片"。

**测试流本身大段静音**：Apple bipbop 是测试图样，帧级静音占比 95% 是正常的，不是解码坏了。拿真实内容测才准。

## 这条路线的边界

- 预取在技术上等于下载音轨，在条款上就是下载。YouTube ToS 禁止复制/下载，Netflix 明确禁止用于 ML 工具开发。
- 所以采集器只做**接口**，不放站点适配器；产物只落本机 `data/`，不提供导出。
- 真要用，建议只对自己有权处理的内容用。

## 下一步

1. 在 B 站 / YouTube 各测一条真实地址，记录倍率与失败原因（403、签名过期、需要 cookie）。
2. 把 `probe_media.py` 的解析层抽成 `media/` 包，供后续下载器复用。
3. 接上 Demucs + Whisper，先跑通"一个本地文件 → 配音音轨"的离线管线。
