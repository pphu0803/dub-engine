# 远程环境探测（rog-windows）
# 用法：ssh rog-windows "powershell -NoProfile -ExecutionPolicy Bypass -File C:\code\dub\probe_env.ps1"
$ErrorActionPreference = "SilentlyContinue"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

function Sec($t) { "`n=== $t ===" }

Sec "OS"
(Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber) | Format-List

Sec "CPU"
(Get-CimInstance Win32_Processor | Select-Object -First 1 Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed) | Format-List

Sec "RAM"
"{0:N1} GB" -f ((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)

Sec "GPU"
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterRAM | Format-List

Sec "Disk"
Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } |
  Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}}, @{n='TotalGB';e={[math]::Round(($_.Used+$_.Free)/1GB,1)}} |
  Format-Table -AutoSize

Sec "Tools"
foreach ($c in @("python", "py", "pip", "git", "ffmpeg", "ffprobe", "node", "uv", "winget", "wsl")) {
  $p = Get-Command $c -ErrorAction SilentlyContinue
  if ($p) { "{0,-8} {1}" -f $c, $p.Source } else { "{0,-8} (missing)" -f $c }
}

Sec "Python versions"
py -0p 2>&1
python --version 2>&1

Sec "ffmpeg (common locations)"
@("C:\ffmpeg\bin\ffmpeg.exe", "C:\Program Files\ffmpeg\bin\ffmpeg.exe", "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ffmpeg.exe") |
  ForEach-Object { if (Test-Path $_) { "found: $_" } }

Sec "ROCm / HIP"
@("C:\Program Files\AMD\ROCm", "C:\Program Files\AMD\ROCm\*\bin\hipInfo.exe") | ForEach-Object {
  if (Test-Path $_) { "found: $_" }
}
Get-ChildItem "C:\Program Files\AMD" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name

Sec "WSL"
wsl -l -v 2>&1

Sec "Network"
foreach ($u in @("https://www.bilibili.com", "https://huggingface.co", "https://pypi.org")) {
  try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-WebRequest -Uri $u -Method Head -TimeoutSec 8 -UseBasicParsing
    "{0,-32} {1} {2}ms" -f $u, $r.StatusCode, $sw.ElapsedMilliseconds
  } catch {
    "{0,-32} FAIL {1}" -f $u, $_.Exception.Message
  }
}

Sec "GPU task manager counters"
try {
  (Get-Counter "\GPU Engine(*)\Utilization Percentage" -ErrorAction Stop).CounterSamples |
    Where-Object { $_.CookedValue -gt 0 } | Select-Object -First 3 InstanceName, CookedValue | Format-Table
} catch { "counter unavailable" }

"`n=== DONE ==="
