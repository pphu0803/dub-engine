# Changelog

## v0.6.0 — 2026-11-01

First public release on GitHub.

### Engine

- Segmented streaming pipeline (~60 s segments): segment 0 starts playing first, the rest is processed while you watch; playback-position-driven scheduling dubs wherever you seek
- Pipeline: Demucs vocal separation → silero VAD → Whisper large-v3-turbo → CAM++ speaker clustering + F0 profiling → LLM translation (time-window budgeted) → sentence-aligned mixing with the background preserved
- Quality: uncovered regions replay original audio (no silent holes); speaker profile consolidation prevents mid-video voice switching; sentence splits snap to real ASR boundaries
- Multi-speaker voices: gender/age-matched voice assignment, per-provider voice maps with pool rotation and pitch-down post-processing (works for every provider)
- Output languages: 中文 / English / 日本語 / 한국어
- TTS provider abstraction: built-in `edge` / OpenAI-compatible / `mimo` / `cosyvoice` / `stub`; config-driven with hot reload and test-listen from the console
- yt-dlp page-mode fallback (optional `cookies` passthrough for login-required pages); downloads cached by content hash
- Management API: `/api/health`, `GET|POST /api/config` (hot reload), `/api/config/test`, `/api/jobs`, `DELETE /api/job/{id}` with an Origin gate for config endpoints
- Resumable jobs (`state.json`), per-segment failure isolation, content-hash job keys (repeat requests return instantly)

### Extension (MV3, Chrome / Edge)

- Content-script-only architecture: no background worker, no special permissions
- Toolbar popup: engine status light, current job progress, dub language, UI language, engine port, per-site button toggle, "Dub this page"
- Management console: engine status, LLM/TTS provider config with live testing, extension preferences, job list with preview / delete
- Bilingual UI (中文 / English), auto-detected from the browser language, switchable anytime
- Engine port configurable in the popup — no code changes needed
- In-page buttons hidden by default (per-site opt-in); auto-continue protection detects media switches by duration

## v0.5 — 2026-10-31

Initial working product: full local pipeline (Demucs → Whisper → LLM → TTS → aligned mixing), segmented streaming playback, browser extension v0.1, content-hash caching, speaker diarization, first real-machine acceptance on Bilibili.
