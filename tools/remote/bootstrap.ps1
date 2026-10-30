# bootstrap.ps1 — 在 rog-windows 上把运行环境装好
# 用法（从本地）：
#   scp tools\remote\bootstrap.ps1 rog-windows:C:/dub/bootstrap.ps1
#   ssh rog-windows "powershell -NoProfile -ExecutionPolicy Bypass -File C:\dub\bootstrap.ps1"
#
# 装什么：
#   * Python 3.12（用户级安装到 C:\dub\python，不改 PATH，不碰系统 Python）
#   * 项目依赖（requests / av）
# 不装什么：
#   * 不装 CUDA / ROCm（这台是 Radeon 8060S，走 CPU 或 DirectML，另议）

$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = "C:\dub"
$PyDir = "$Root\python"
$PyExe = "$PyDir\python.exe"
$PyVer = "3.12.10"
$Installer = "$Root\python-$PyVer-amd64.exe"

New-Item -ItemType Directory -Force $Root | Out-Null

if (-not (Test-Path $PyExe)) {
    Write-Host "[1/3] 下载 Python $PyVer ..."
    if (-not (Test-Path $Installer)) {
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PyVer/python-$PyVer-amd64.exe" -OutFile $Installer -UseBasicParsing
    }
    Write-Host "[2/3] 静默安装到 $PyDir ..."
    $args = @(
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=$PyDir",
        "Include_pip=1",
        "Include_launcher=0",
        "Include_test=0",
        "Include_doc=0",
        "PrependPath=0",
        "Shortcuts=0"
    )
    $p = Start-Process -FilePath $Installer -ArgumentList $args -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "安装器退出码 $($p.ExitCode)" }
} else {
    Write-Host "[1/3] Python 已存在，跳过"
    Write-Host "[2/3] 已存在，跳过"
}

if (-not (Test-Path $PyExe)) { throw "安装后仍找不到 $PyExe" }

Write-Host "[3/3] 安装依赖 ..."
& $PyExe -m pip install --upgrade pip --quiet
& $PyExe -m pip install -r "$Root\dub-engine\requirements.txt" --quiet

Write-Host ""
Write-Host "=== 环境就绪 ==="
& $PyExe --version
& $PyExe -c "import requests, av; print('requests', requests.__version__); print('av', av.__version__)"
Write-Host "python: $PyExe"
