# probe_env2.ps1 — 找 ffmpeg / ROCm / ComfyUI / conda，确认哪个 Python 能用 GPU
$ErrorActionPreference = "SilentlyContinue"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

function Sec($t) { "`n=== $t ===" }

Sec "ffmpeg in PATH"
where.exe ffmpeg 2>&1
where.exe ffprobe 2>&1

Sec "drives"
Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } |
  Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}}, @{n='Root';e={$_.Root}} | Format-Table -AutoSize

Sec "ffmpeg on disks (depth<=3)"
$roots = @("C:\", "D:\", "E:\", "F:\", "G:\", "H:\")
foreach ($r in $roots) {
  if (-not (Test-Path $r)) { continue }
  Get-ChildItem -Path $r -Filter ffmpeg.exe -Recurse -Depth 3 -File -ErrorAction SilentlyContinue |
    Select-Object -First 5 -ExpandProperty FullName
}

Sec "ROCm / AMD"
Get-ChildItem "C:\Program Files\AMD" -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name
@("C:\Program Files\AMD\ROCm", "C:\Program Files\AMD\ROCm\6.4", "C:\Program Files\AMD\ROCm\7.0") |
  ForEach-Object { if (Test-Path $_) { "found: $_" } }

Sec "ComfyUI / conda on external disks"
foreach ($r in @("F:\", "G:\", "H:\")) {
  if (-not (Test-Path $r)) { continue }
  Get-ChildItem -Path $r -Directory -Depth 2 -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "ComfyUI|miniconda|anaconda|Miniforge|python_embeded|venv" } |
    Select-Object -First 20 -ExpandProperty FullName
}

Sec "python.exe under ComfyUI-ish paths"
foreach ($r in @("F:\", "G:\", "H:\")) {
  if (-not (Test-Path $r)) { continue }
  Get-ChildItem -Path $r -Filter python.exe -Recurse -Depth 4 -File -ErrorAction SilentlyContinue |
    Select-Object -First 10 -ExpandProperty FullName
}

Sec "conda envs (if conda on PATH)"
where.exe conda 2>&1

"`n=== DONE ==="
