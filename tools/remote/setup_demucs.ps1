# setup_demucs.ps1 — 建隔离 venv，装 demucs（不污染 ComfyUI 环境），验证 ROCm 可用
$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$Py = "H:\SHARE\ComfyUI\miniconda3\envs\comfyui\python.exe"
$Venv = "C:\code\dub\venv"
$VenvPy = "$Venv\Scripts\python.exe"

Write-Host "[1/4] 建 venv（--system-site-packages，复用 conda 里的 torch）"
if (-not (Test-Path $VenvPy)) {
    & $Py -m venv $Venv --system-site-packages
}

Write-Host "[2/4] 装 demucs（--no-deps，避免动 torch）"
& $VenvPy -m pip install --quiet demucs --no-deps einops

Write-Host "[3/4] 校验 torch / ROCm"
& $VenvPy -c "import torch; print('torch', torch.__version__); print('cuda_available', torch.cuda.is_available()); print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'n/a'); print('hip', torch.version.hip)"

Write-Host "[4/4] 校验 demucs 导入"
& $VenvPy -c "import demucs; from demucs.pretrained import get_model; print('demucs ok', demucs.__version__)"
