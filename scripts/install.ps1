# install.ps1 — 环境安装脚本（以管理员或普通用户运行均可）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\install.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== Video Pipeline 环境安装 ===" -ForegroundColor Cyan

# 1. Python 版本检查
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[错误] 未找到 Python。请安装 Python 3.11+ 并加入 PATH。" -ForegroundColor Red
    exit 1
}
$ver = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "[OK] Python $ver"

# 2. Python 依赖
Write-Host "安装 Python 依赖..."
python -m pip install --upgrade pip
python -m pip install -r "$PSScriptRoot\..\requirements.txt"

# 3. FFmpeg 检查
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "[提示] 未找到 FFmpeg，尝试用 winget 安装..."
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
} else {
    Write-Host "[OK] FFmpeg 已安装"
}

# 4. DeepFilterNet（可选，音频降噪）
$installDfn = Read-Host "是否安装 DeepFilterNet（CPU 音频降噪）? [y/N]"
if ($installDfn -eq "y") {
    python -m pip install deepfilternet
}

# 5. REAL-Video-Enhancer 需要从其官网/GitHub 单独下载安装
Write-Host ""
Write-Host "[提示] REAL-Video-Enhancer 请从其官方发布页下载安装，" -ForegroundColor Yellow
Write-Host "       安装后在 config.yaml 的 video_repair.executable 中配置路径。" -ForegroundColor Yellow

Write-Host ""
Write-Host "=== 安装完成，运行 doctor 检查环境 ==="
python "$PSScriptRoot\..\main.py" doctor
