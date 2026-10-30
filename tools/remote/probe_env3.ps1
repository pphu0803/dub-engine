# probe_env3.ps1 — 检查 ComfyUI 的 miniconda 里有没有 torch/ROCm，并找 ffmpeg
$ErrorActionPreference = "SilentlyContinue"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

function Sec($t) { "`n=== $t ===" }

$Conda = "H:\SHARE\ComfyUI\miniconda3"
$Py = "$Conda\python.exe"

Sec "conda envs"
& "$Conda\Scripts\conda.exe" env list 2>&1

Sec "base python"
& $Py -c "import sys; print(sys.version)" 2>&1

Sec "torch in base"
& $Py -c "import torch; print('torch', torch.__version__); print('cuda', torch.version.cuda); print('hip', torch.version.hip); print('avail', torch.cuda.is_available()); print('dev', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'n/a')" 2>&1

Sec "key packages"
& $Py -m pip list 2>&1 | Select-String -Pattern "torch|numpy|av|whisper|demucs|onnx|transformers|soundfile|librosa|scipy" | Select-Object -First 20

Sec "ffmpeg deep search (H: ComfyUI)"
Get-ChildItem -Path "H:\SHARE" -Filter ffmpeg.exe -Recurse -Depth 6 -File -ErrorAction SilentlyContinue |
  Select-Object -First 5 -ExpandProperty FullName

Sec "ffmpeg anywhere on F/G/H (depth 5)"
foreach ($r in @("F:\", "G:\", "H:\")) {
  Get-ChildItem -Path $r -Filter ffmpeg.exe -Recurse -Depth 5 -File -ErrorAction SilentlyContinue |
    Select-Object -First 3 -ExpandProperty FullName
}

Sec "ROCm inside ComfyUI"
Get-ChildItem -Path "H:\SHARE\ComfyUI" -Directory -Depth 2 -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match "rocm|hip" } | Select-Object -ExpandProperty FullName

"`n=== DONE ==="
