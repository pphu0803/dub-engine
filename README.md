# dub-engine — Real-time AI Dubbing for Web Videos

[English](README.md) | [简体中文](README.zh-CN.md)

> Open any video site, one click, and the video speaks your language — no downloads, no editing, right in the browser.

Click "🎙 AI Dub" on a video page, and about two minutes later the dubbing starts playing: **the original background music and sound effects are preserved untouched — only the dialogue is replaced** with your target language, aligned to the pacing of the picture. All processing runs on your own machine, streaming in segments: after a short wait the original audio is muted and the AI-dubbed version takes over, with later segments finishing while you watch.

## What it does

1. **Captures the audio track** — the extension finds the current video's audio stream URL automatically (no full video download)
2. **Local pipeline** — vocal separation (Demucs) → speech recognition (Whisper) → LLM translation → AI dubbing → sentence-aligned mixing, all on your GPU, segment by segment
3. **Streams back** — playback starts as soon as the first segment is done; with a discrete GPU, processing (~45 s per minute of audio) stays ahead of playback
4. **Switch back anytime** — one click between original audio and dubbing; seek freely

## Features

- **Near real-time**: segmented streaming architecture (~60 s segments); no waiting for the whole video
- **Rhythm alignment**: sentence-level splitting; each sentence starts at the original line's timestamp and its speed is stretched to fit the next line — dialogue follows the performance
- **Multiple speakers**: voiceprint clustering + F0 profile merging (a character never changes voice mid-video); each speaker gets a voice matched to their gender and age
- **Background preserved**: vocal separation swaps only the dialogue — music, ambience and SFX stay; unrecognized lines automatically fall back to the original audio, so there are no silent holes
- **Local-first**: audio/video never leaves your machine; only dialogue text is sent to the LLM you configure
- **Multiple output languages**: 中文 / English / 日本語 / 한국어, switchable in the extension; each language is cached independently
- **Resumable**: re-dubbing the same video returns instantly; a failed segment doesn't affect the rest — resume with one click
- **Browsers**: Chrome / Edge (MV3 extension)

## Current limitations

- **Sites**: Bilibili and YouTube are primary (the extension sniffs direct audio URLs); when no direct URL is found it automatically falls back to **yt-dlp page mode** — the engine fetches the audio from the page itself, covering in principle every site yt-dlp supports. Pages that require a login (e.g. VIP content) can pass `"cookies": "chrome|edge|firefox"` in the request to reuse your browser session (untested)
- **Output languages**: 中文 / English / 日本語 / 한국어 (adding a language is two rows in the `dub/stages.py` voice table); fewer voices available for ja/ko (no child voices)
- **Hardware**: needs a local GPU (tested on AMD ROCm; should work on NVIDIA; CPU-only is too slow for watch-while-processing)
- **First run**: downloads models (Whisper ~1.6 GB, Demucs ~80 MB, speaker model ~28 MB, automatically)
- Translation quality depends on the LLM you configure; vocals may leak through separation in scenes with continuous loud BGM

---

## Requirements

| Dependency | Requirement | Notes |
|---|---|---|
| Python | 3.10 – 3.12 | Developed on 3.12 |
| PyTorch | Build matching your GPU | NVIDIA: CUDA build; AMD: ROCm build; see install steps |
| GPU | NVIDIA / AMD discrete or integrated | Vocal separation and ASR run on GPU; CPU-only works but is too slow for viewing |
| FFmpeg | **Not required** | The engine uses the binary bundled with pip (imageio-ffmpeg) |
| LLM API key | Any OpenAI-compatible endpoint | DeepSeek, Qwen, Kimi, OpenAI, etc. |
| Browser | Chrome or Edge | Load the MV3 extension |

No system dependencies; everything is installed via pip.

## Installation

### 1. Clone

```bash
git clone https://github.com/pphu0803/dub-engine.git
cd dub-engine
```

### 2. (Recommended) Create a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 3. Install PyTorch (pick the one for your platform)

```bash
# NVIDIA (CUDA 12.x)
pip install torch --index-url https://download.pytorch.org/whl/cu121

# AMD (ROCm 6.x, Linux; AMD on Windows needs a ROCm 7.2+ preview wheel)
pip install torch --index-url https://download.pytorch.org/whl/rocm6.2

# CPU only (works, but too slow for watch-while-processing)
pip install torch
```

### 4. Install the remaining dependencies

```bash
pip install -r requirements.txt
```

### 5. Model downloads (automatic on first run; you can pre-warm with a proxy)

| Model | Size | Source |
|---|---|---|
| Whisper large-v3-turbo (ASR) | ~1.6 GB | HuggingFace |
| Demucs htdemucs (vocal separation) | ~80 MB | Automatic |
| CAM++ (speaker voiceprint) | ~28 MB | ModelScope |

> In China, set the HuggingFace mirror before starting:
> ```bash
> # Windows (PowerShell)
> $env:HF_ENDPOINT = "https://hf-mirror.com"
> # Linux / macOS
> export HF_ENDPOINT=https://hf-mirror.com
> ```

## Configure the LLM (management page, or three server-side ways)

The engine needs an OpenAI-compatible LLM to translate dialogue. **Server-side priority: CLI args > environment variables > config.json**.

### Way ⓪ — Management page (recommended, no restart)

After installing the extension (below): click the toolbar icon → "⚙ Management page" → "Translation LLM" → fill base / key / model → "Test connection" (makes one real API call) → "Save". The config is written back to `config.json` and applies to **the next job immediately** — no engine restart needed.

Server-side ways (read at engine startup):

### Way A — Config file

```bash
cp config.example.json config.json
```

Edit `config.json` (gitignored, never committed):

```json
{
  "llm": {
    "base": "https://api.deepseek.com/v1",
    "key": "sk-your-key",
    "model": "deepseek-chat"
  }
}
```

### Way B — Environment variables

```bash
# Windows (PowerShell)
$env:DUB_LLM_BASE = "https://api.deepseek.com/v1"
$env:DUB_LLM_KEY  = "sk-your-key"
$env:DUB_LLM_MODEL = "deepseek-chat"

# Linux / macOS
export DUB_LLM_BASE="https://api.deepseek.com/v1"
export DUB_LLM_KEY="sk-your-key"
export DUB_LLM_MODEL="deepseek-chat"
```

### Way C — CLI arguments

See `--llm-*` in the next section.

## TTS backends (provider-based)

TTS goes through a provider abstraction (`dub/tts_providers.py`): built-ins are `edge` / `openai` / `mimo` / `cosyvoice` / `stub`, all replaceable or extensible via the `tts` section of config.json. **Two ways to add a vendor**:

1. **Zero code**: the vendor exposes an OpenAI-compatible `/v1/audio/speech` endpoint (OpenAI / SiliconFlow / various aggregators) → just configure a `{"type": "openai"}` provider;
2. **Small adapter**: for special protocols (e.g. Xiaomi MiMo's chat/completions format) → a ~50-line subclass registered in `PROVIDER_TYPES`.

```json
{
  "tts_backend": "mimo",
  "tts": {
    "providers": {
      "mimo":   {"type": "mimo",   "base": "https://api.xiaomimimo.com/v1", "key": "...", "model": "mimo-v2.5-tts"},
      "openai": {"type": "openai", "base": "https://api.openai.com/v1", "key": "...", "model": "gpt-4o-mini-tts"},
      "cosy":   {"type": "cosyvoice", "root": "D:/CosyVoice", "model": "...", "ref_wav": "D:/ref/speaker.wav"}
    },
    "voice_maps": {
      "mimo": {
        "zh": {
          "m_young":  [["苏打", 0], ["白桦", 0]],
          "m_mature": [["白桦", -4], ["苏打", -3]],
          "f_mature": [["茉莉", 0]]
        }
      }
    }
  }
}
```

- `tts_backend` = a built-in provider name, or any name defined in `tts.providers`; switch via config / `--tts-backend` / per-request `"tts_backend"` (highest priority) / the management page dropdown (hot reload)
- **Voice maps**: `gender_age` (m/f/x × child/young/mature) → a pool of `[voice, semitones]` pairs, rotated per speaker (same-profile characters get different voices); negative semitones = pitch down (weathered male); the `"*"` pool is the fallback. Built-in tables live in `dub/tts_providers.py` (including ear-calibrated MiMo Chinese voices); a config key with the same name overrides them, editable as JSON on the management page with hot reload
- Per-request override: `POST /api/dub` with `"tts_voice": "苏打"` → single voice for the whole video (voice names are provider-specific literal strings)
- Legacy `mimo` config section / `DUB_MIMO_KEY` migrate automatically to `providers.mimo`
- Management page "Dubbing TTS": pick a provider → fill base/key/model → **test listen** (plays the synthesized audio right on the page)

## Start the engine

```bash
python -m dub serve --port 8765
```

Full example with LLM arguments:

```bash
python -m dub serve --port 8765 \
  --llm-base "https://api.deepseek.com/v1" \
  --llm-key "sk-..." \
  --llm-model "deepseek-chat"
```

Success looks like this in the engine console:

```
[serve] LLM: deepseek-chat @ https://api.deepseek.com/v1
[serve] http://127.0.0.1:8765  (Ctrl+C to exit)
```

**Verify**: open `http://127.0.0.1:8765/api/health` in a browser — `{"ok": true}` means the engine is up.

Notes:

- The server binds **127.0.0.1 only** — never exposed to the network, no firewall prompt
- Keep the console open; closing it stops the engine (results are on disk — the same video returns instantly after a restart)
- On the first video the console prints model download progress and per-stage timings
- To use another port: `--port 8888`, then set the same value in the extension popup's "Engine port" (applies immediately — no code changes)

## Install the browser extension

1. Open `chrome://extensions` (Edge: `edge://extensions`)
2. Enable "Developer mode" → "Load unpacked"
3. Select the `extension/` directory of this repo
4. (Optional) Pin the extension to the toolbar

### Toolbar popup & management page

- **No buttons are shown on web pages by default** (non-video pages stay clean). Click the toolbar icon to open the **popup**:
  - Engine status light (green online / blue busy / red offline), current job progress, default language
  - **Engine port**: keep in sync with `python -m dub serve --port` (default 8765, applies immediately)
  - **🎬 Dub this page**: start dubbing the current tab with one click (works even without in-page buttons)
  - **Show dub buttons on this site**: enables the in-page "AI Dub / language" buttons for this site (remembered per site)
  - **Progress pill**: toggle for the on-page progress capsule (errors still show when off)
- The popup's "⚙ Management page" (or right-click the icon → "Options") opens the full management page:
  - **Engine status**: version / pipeline / backend config state / uptime
  - **Translation LLM / TTS providers**: fill config → test → save; hot reload, no restart
  - **Extension preferences**: default target language (unified across sites), progress pill toggle
  - **Job list**: recent jobs (incl. history) with title / language / backend / progress / duration; listen to results, delete with double confirm

## Usage

### Dub a video

1. Open a Bilibili video page and **play for 2 seconds** — the player then exposes the audio URL, which the extension picks up
2. Click "🎬 Dub this page" in the popup, or enable "Show dub buttons on this site" first and click the in-page "🎙 AI Dub"
3. The progress capsule shows: `reading page → queued → extracting audio → dubbing N/M segments`
   (each segment runs: Demucs separation → Whisper ASR → LLM translation → TTS → aligned mixing)
4. **Playback starts automatically when the first segment is done** (first run takes a few minutes due to model downloads), the rest streams in as you watch
5. The original video is muted; the background music stays in the dub track

> If the in-page audio URL can't be sniffed (player variants), the extension automatically falls back to the engine's **yt-dlp page mode** (the capsule shows "yt-dlp"). For login-required pages (VIP) you can POST directly to the engine:
>
> ```bash
> curl -X POST http://127.0.0.1:8765/api/dub -H "Content-Type: application/json" \
>   -d '{"page_url":"https://www.bilibili.com/video/BV...","lang":"zh","cookies":"chrome"}'
> ```

### In-page buttons (hidden by default; enable per site in the popup)

While a dubbing session is running, the buttons appear automatically (you need them to switch back to original audio).

| Control | Action |
|---|---|
| 🌐 Language dropdown | Output language for dubbing (中文/EN/JA/KO); remembered; applies on next click |
| 🎙 AI Dub | Capture the audio track and start the pipeline |
| 🔈 Original audio / 🔊 Back to dub | One-click swap, progress preserved |
| 🎙 Re-dub | Resume failed segments; a fresh job is created after alignment upgrades |

### Seeking

Seek anywhere — **the engine dubs wherever you are**: it receives your playback position in real time and prioritizes the segments nearest you; you don't wait for earlier segments. Seeking into an unprocessed segment falls back to original audio (never silence); that segment switches to dubbing automatically once ready in the background.

### Auto-continue

When a site auto-plays the next episode, the extension detects the media switch by video duration, immediately stops the previous job, restores original audio and tells you — the second episode never plays the first episode's voice. Click "AI Dub" to start the new video.

### Caching & resume

- Clicking the same video again: instant return of the finished dubbing (cached by audio content hash, URL-independent)
- Closing the page or the engine loses nothing — clicking again resumes from the breakpoint
- A failed segment (e.g. network hiccup) doesn't affect the rest: it reports "N/M segments done", and "Re-dub" retries only the failed ones

### Troubleshooting

| Symptom | Fix |
|---|---|
| ❌ Can't reach the engine | Engine not started or wrong port; check `http://127.0.0.1:8765/api/health` first |
| ❌ No audio-like request found | Refresh the page, play 2 seconds, then click; the extension falls back to yt-dlp page mode — if that also fails, check the engine console |
| ❌ yt-dlp fails / Unsupported URL | Site unsupported by yt-dlp or network down; pass `cookies` for VIP content; see engine console for details |
| ❌ Segment N: timeout / ConnectionError | Network jitter — click "Re-dub" to resume; LLM calls already retry 4× on a direct connection |
| First click is slow | Downloading the Whisper model (1.6 GB), watch the engine console; set `HF_ENDPOINT` mirror in China |
| All dialogue in one voice | Speaker model failed to download (see engine log); clicking again retries |
| 403 / extension fails to load | Make sure the `extension` directory is complete (incl. manifest.json); use "Reload", not repeated installs |

## How it works

```
Browser extension ──audio URL──▶ Local engine (127.0.0.1:8765)
                                    │
                                    ▼
        Split at ~60 s silence points, pipelined per segment (segment 0 first):
        Demucs vocal separation → VAD → Whisper ASR → speaker voiceprint clustering
        → LLM translation (length-budgeted to the time window) → F0-profile voice matching TTS
        → sentence-aligned mixing (background kept, each line fits the original rhythm)
                                    │
Extension ◀──segment mp3──────── segment 0 returns first; watch while it processes
```

- **Alignment**: long segments are split by sentence with cut points snapped to real Whisper boundaries (not interpolated guesses); each sentence starts at the original start and its speed fits the next sentence — dialogue follows the performance
- **Speakers**: voiceprint embeddings + hierarchical clustering, consistent numbering across segments; each speaker is matched to a dubbing voice by fundamental frequency / timbre (gender and age band match)
- **Hallucination filter**: VAD segmentation + text loop detection; music segments won't produce "Thanks for watching" ghost lines
- **Caching**: jobs are keyed by audio content hash + target language; repeated requests return instantly; interrupted jobs resume

## Project structure

```
dub/            Engine: pipeline, segment scheduling, speakers, voice profiling, HTTP service
extension/      Browser extension (MV3, content-script only, no background, zero extra permissions)
tools/          Audio probing and helper scripts
config.example.json   LLM config template (copy to config.json)
```

## Roadmap

- [ ] More sites: full YouTube support, generic HLS/m3u8, generic web auto-adaptation
- [x] Multiple target languages (zh/en/ja/ko, selectable in the extension)
- [ ] Voice cloning: keep the original speaker's timbre in the dub
- [ ] Lip-sync alignment (phoneme-level timestamps)
- [ ] Installer packaging / Firefox support

## Acknowledgments

- [Demucs](https://github.com/adefossez/demucs) (Meta, MIT) — music/vocal separation
- [Whisper](https://github.com/openai/whisper) (OpenAI, MIT) — speech recognition
- [silero-vad](https://github.com/snakers4/silero-vad) (MIT) — voice activity detection
- [3D-Speaker / CAM++](https://github.com/modelscope/3D-Speaker) (Apache-2.0) — speaker embeddings
- [edge-tts](https://github.com/rany2/edge-tts) (GPL-3.0) — speech synthesis
- [Librosa](https://github.com/librosa/librosa) (ISC) — audio processing

## License

[MIT](LICENSE)

> Please use responsibly: for personal use, and respect the terms of service of the sites you watch and the copyright of the content you dub.
