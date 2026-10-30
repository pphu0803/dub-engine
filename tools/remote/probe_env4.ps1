# probe_env4.ps1 — 看 comfyui 环境的 torch 后端，以及 ffmpeg 到底在哪
$ErrorActionPreference = "SilentlyContinue"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

function Sec($t) { "`n=== $t ===" }

$Root = "H:\SHARE\ComfyUI\miniconda3"
$EnvPy = "$Root\envs\comfyui\python.exe"

Sec "comfyui env python"
& $EnvPy -c "import sys; print(sys.version)" 2>&1

Sec "comfyui torch"
& $EnvPy -c "import torch; print('torch', torch.__version__); print('hip', torch.version.hip); print('cuda', torch.version.cuda); print('avail', torch.cuda.is_available()); print('name', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'n/a'); print('vram', round(torch.cuda.get_device_properties(0).total_memory/2**30,1) if torch.cuda.is_available() else 'n/a')" 2>&1

Sec "comfyui packages"
& $EnvPy -m pip list 2>&1 | Select-String -Pattern "^torch|^numpy|^av\s|whisper|demucs|onnxruntime|transformers|soundfile|librosa|scipy|imageio" | Select-Object -First 25

Sec "ffmpeg binaries under miniconda"
Get-ChildItem -Path $Root -Filter "ffmpeg*.exe" -Recurse -Depth 7 -File -ErrorAction SilentlyContinue |
  Select-Object -First 10 -ExpandProperty FullName

Sec "any ffmpeg*.exe on H: (depth 7)"
Get-ChildItem -Path "H:\" -Filter "ffmpeg*.exe" -Recurse -Depth 7 -File -ErrorAction SilentlyContinue |
  Select-Object -First 10 -ExpandProperty FullName

Sec "all drives ffmpeg via where /r (may be slow)"
cmd /c "where /r C:\ ffmpeg.exe 2>nul" | Select-Object -First 5

"`n=== DONE ==="
