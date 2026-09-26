<#
.SYNOPSIS
    为 video_pipeline 安装并集成两个 AI 组件：
      - 画质增强：REAL-Video-Enhancer 2.4.1（后端 CLI 直驱，绕开 GUI）
      - 音质修复：DeepFilterNet 0.5.6（CPU）

.DESCRIPTION
    为什么需要两个独立虚拟环境？
      REAL-Video-Enhancer 后端依赖 numpy 2.x；DeepFilterNet 依赖 numpy < 2。
      二者无法共存于同一解释器，因此分别使用 .venv-rve 与 .venv。

    为什么直驱 rve-backend.py 而不是 RVE 的 exe？
      RVE 的 Windows 发行版是 PyQt GUI，无法无人值守调用；但其推理后端
      backend/rve-backend.py 是标准 argparse CLI，本脚本部署该后端，
      由 config.yaml 的 args_template 直接驱动。

.PARAMETER Cuda
    为 RVE 安装 CUDA 版 torch（需 NVIDIA GPU + 驱动）。默认安装 CPU 版。
    CPU 版可用于功能验证，但整集长视频修复会非常慢
    （实测 2 秒视频约 6-9 分钟，整集需数十小时）。

.CUDAVERSION
    CUDA 构建默认 cu128，对应版本组合：
        torch 2.7.0+cu128 / torchvision 0.22.0+cu128 / cp311 / win_amd64
    选择依据：
      * 与 RVE 官方 tools/backend/requirements.txt 的 pin 一致（torch==2.7.0）；
      * 需要 NVIDIA 驱动 >= 570（cu128 = CUDA 12.8 runtime）。
        RTX 40 系（sm_89）原生支持。
    驱动较旧时改用 -CudaIndex：
        cu126 → 驱动 >= 560 ；cu118 → 驱动 >= 452（老卡兼容最好）

.PARAMETER CudaIndex
    CUDA 构建标签，默认 cu128。可选 cu128 / cu126 / cu118。

.PARAMETER PipCacheDir
    pip 下载缓存与下载临时目录。默认放在**项目同级目录**的 .pip-cache，
    避免 CUDA 轮子（单个 3.3 GB）把 C 盘临时空间吃掉。
    传空字符串外的路径可自定义（建议放在空间较大的盘，避免占用系统盘）。

.PARAMETER RveVersion
    REAL-Video-Enhancer 版本，默认 2.4.1。

.EXAMPLE
    # 功能验证（CPU）
    powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1

    # 生产（CUDA 12.8）
    powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda

    # 老驱动（CUDA 12.6）
    powershell -ExecutionPolicy Bypass -File scripts\setup_ai_tools.ps1 -Cuda -CudaIndex cu126

.NOTES
    - 幂等：已存在则跳过，可反复执行。
    - 需要外网（PyPI / download.pytorch.org / GitHub Releases）。
      CUDA 轮子下载慢时脚本会自动切到国内镜像（多镜像测速 + 断点续传）。
    - 虚拟环境本体始终建在项目目录内（.venv / .venv-rve）。
      注意：venv **不可直接拷贝迁移**（pyvenv.cfg 与 Scripts/*.exe 内写死了
      绝对路径）；换机器请重跑本脚本，而不是复制 venv 目录。
    - 本脚本只做安装与集成，不修改流水线业务逻辑。
#>
[CmdletBinding()]
param(
    [switch]$Cuda,
    [string]$RveVersion = "2.4.1",
    [string]$PipCacheDir = "",
    [string]$CudaIndex   = "cu128"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --------------------------------------------------------------------------- #
# 版本选择依据
#   * .venv     (DeepFilterNet) 固定 torch/torchaudio 2.5.1 —— 实测可用，
#               torchaudio 的 C++ 算子注册正常（见 §torchaudio 混装说明）。
#   * .venv-rve (REAL-Video-Enhancer) 跟随 RVE 官方 tools/backend/requirements.txt：
#               torch==2.7.0 / torchvision==0.22.0。CUDA 构建选 cu128，
#               对应 NVIDIA 驱动 >= 570（本机 572.83，最高支持 CUDA 12.8）。
#               若显存/驱动较旧，可改 -CudaIndex cu126 或 cu118。
# --------------------------------------------------------------------------- #
$TORCH_VER_DF  = "2.5.1"    # DeepFilterNet 侧
$TV_VER_DF     = "0.20.1"
$TORCH_VER_RVE = "2.7.0"    # RVE 侧（对齐 RVE 官方 pin）
$TV_VER_RVE    = "0.22.0"
$DF_VER        = "0.5.6"

# --------------------------------------------------------------------------- #
# 安装位置策略（用户诉求：项目目录 > D 盘 > C 盘）
#   * 虚拟环境本体：始终建在**项目目录内**（.venv / .venv-rve），
#     随项目走，不散落到 C 盘。
#   * pip 下载缓存 & 下载临时目录：默认落在 C 盘（%LOCALAPPDATA%\pip\Cache、
#     %TEMP%）。CUDA 轮子单个 3.3 GB，会把 C 盘临时吃掉好几 GB，
#     因此这里显式重定向到项目同级目录。
# --------------------------------------------------------------------------- #
if (-not $PipCacheDir) {
    $PipCacheDir = Join-Path (Split-Path -Parent $Root) ".pip-cache"
}
$PipTempDir = Join-Path $PipCacheDir "tmp"
New-Item -ItemType Directory -Force -Path $PipCacheDir, $PipTempDir | Out-Null
$env:PIP_CACHE_DIR = $PipCacheDir
$env:TEMP = $PipTempDir
$env:TMP  = $PipTempDir

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!]  $msg"   -ForegroundColor Yellow }

function Get-Python {
    # 依次尝试：PATH 上的解释器 → 由环境变量推导的常见安装位置。
    # 这里刻意不写死任何盘符或版本号，换机器无需改脚本。
    foreach ($name in @("python", "python3", "py")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    $cands = @()
    if ($env:USERPROFILE) {
        $cands += Get-ChildItem `
            "$env:USERPROFILE\.workbuddy\binaries\python\versions\*\python.exe" `
            -ErrorAction SilentlyContinue
    }
    if ($env:LOCALAPPDATA) {
        $cands += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" `
            -ErrorAction SilentlyContinue
    }
    foreach ($c in $cands) { if ($c) { return $c.FullName } }
    throw "未找到 python，请先安装 Python 3.11 并加入 PATH，然后重跑本脚本"
}

function New-Venv($path, $python) {
    if (Test-Path "$path\Scripts\python.exe") { Ok "$path 已存在"; return }
    Write-Host "  创建虚拟环境 $path ..."
    & $python -m venv $path
    if (-not (Test-Path "$path\Scripts\python.exe")) { throw "创建 $path 失败" }
    Ok "$path 创建完成"
}

function Pip($venv, [string[]]$pkgs) {
    $py = "$venv\Scripts\python.exe"
    Write-Host "  pip install $($pkgs -join ' ')"
    & $py -m pip install --upgrade pip --quiet
    & $py -m pip install @pkgs
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败: $($pkgs -join ' ')" }
}

# --------------------------------------------------------------------------- #
Step "0/6 预检"
$PY = Get-Python
Write-Host "  python: $PY"
& $PY -c "import sys; assert sys.version_info>=(3,11), 'need >=3.11'; print('  版本', sys.version.split()[0])"

$FFMPEG = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($FFMPEG) { Ok "ffmpeg: $($FFMPEG.Source)" }
else { Warn "PATH 上未找到 ffmpeg；RVE 需要 ffmpeg，请在 config.yaml 的 video_repair.ffmpeg_path 指定绝对路径" }

# --------------------------------------------------------------------------- #
Step "1/6 创建两个隔离虚拟环境"
New-Venv ".venv"     $PY
New-Venv ".venv-rve" $PY

# --------------------------------------------------------------------------- #
Step "2/6 安装流水线运行时依赖 (PyYAML / psutil / pytest)"
Pip ".venv" @("PyYAML>=6.0", "psutil>=5.9", "pytest>=7.4")

# --------------------------------------------------------------------------- #
Step "3/6 安装 DeepFilterNet ($DF_VER) 及其运行时"
Pip ".venv" @("deepfilternet==$DF_VER")
# torchaudio 需要 soundfile 后端才能读写 WAV
Pip ".venv" @("soundfile>=0.10,<0.13")

# --- torch / torchaudio（关键：必须同源同版本，否则 import 直接崩） ---------- #
# 踩坑记录：
#  1) torch 是 df.enhance 的**实际依赖**，但 PyPI 元数据漏标，必须显式安装。
#  2) 若 torch 与 torchaudio 来自不同来源/版本，或上一次安装被中断在
#     site-packages 留下 `~torchaudio-*.dist-info` 这类残目录，会出现：
#        AttributeError: '_OpNamespace' '_torchaudio' object has no attribute 'cuda_version'
#     （旧 .py 与新 .pyd 混装，torchaudio 的 C++ 算子未注册到 torch.ops）
#  3) 因此这里先清理 `~*` 残目录，再统一从 PyTorch 官方 CPU 索引 force-reinstall。
function Clear-PipLeftovers([string]$sitePackages) {
    if (-not (Test-Path $sitePackages)) { return }
    Get-ChildItem -Path $sitePackages -Filter "~*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Warn "清理上次中断安装的残留: $($_.Name)"
            try { [System.IO.Directory]::Delete($_.FullName, $true) } catch { }
        }
}
Clear-PipLeftovers ".venv\Lib\site-packages"

$pyDF = ".\.venv\Scripts\python.exe"
& $pyDF -m pip install --force-reinstall --no-deps "torch==$TORCH_VER_DF" "torchaudio==$TORCH_VER_DF" --index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE -ne 0) { throw "torch / torchaudio 安装失败" }

# DeepFilterNet 要求 numpy < 2（RVE 侧才需要 numpy 2.x，故分环境安装）
Pip ".venv" @("numpy<2")

& $pyDF -c "import torch, torchaudio, soundfile, df; print('  DeepFilterNet 就绪, torch', torch.__version__, '| torchaudio', torchaudio.__version__); torchaudio.lib._torchaudio.cuda_version(); print('  torchaudio 扩展算子注册正常')"

# --------------------------------------------------------------------------- #
Step "4/6 部署 REAL-Video-Enhancer 后端源码 (v$RveVersion)"
New-Item -ItemType Directory -Force -Path "tools" | Out-Null
if (Test-Path "tools\backend\rve-backend.py") {
    Ok "tools\backend 已存在"
} else {
    $tar = "tools\backend-v$RveVersion.tar.gz"
    $url = "https://github.com/TNTwise/REAL-Video-Enhancer/releases/download/RVE-$RveVersion/backend-v$RveVersion.tar.gz"
    Write-Host "  下载 $url"
    Invoke-WebRequest -Uri $url -OutFile $tar -UseBasicParsing
    # tar 在 Windows 10+ 自带；解压到 tools/ 得到 tools/backend/*
    & tar -xzf $tar -C tools
    if (-not (Test-Path "tools\backend\rve-backend.py")) { throw "解压后未找到 rve-backend.py" }
    Ok "RVE 后端已部署"
}

# --------------------------------------------------------------------------- #
Step "5/6 安装 RVE 后端依赖 (pytorch 后端, 超分+压缩修复路径)"
$rvePkgs = @(
    "opencv-python-headless",
    "requests", "einops", "safetensors",
    "tqdm", "sympy", "typing_extensions", "packaging", "pillow"
)
Pip ".venv-rve" $rvePkgs

if ($Cuda) {
    $py = ".\.venv-rve\Scripts\python.exe"
    $cudaUrl = "https://download.pytorch.org/whl/$CudaIndex"
    Warn "安装 CUDA 版 torch $TORCH_VER_RVE+$CudaIndex（约 3.4 GB）"
    Write-Host "  缓存/临时目录: $PipCacheDir（已重定向，不占 C 盘）"

    & $py -m pip install --no-deps --index-url $cudaUrl `
        "torch==$TORCH_VER_RVE" "torchvision==$TV_VER_RVE"

    if ($LASTEXITCODE -ne 0) {
        Warn "官方源失败或过慢，切换国内镜像（多镜像测速 + 断点续传）"
        $dl = Join-Path $PipCacheDir "wheels"
        & $py "scripts\fetch_cuda_wheels.py" --dest $dl --cuda-index $CudaIndex
        if ($LASTEXITCODE -ne 0) { throw "CUDA 轮子下载失败" }
        $whls = @(Get-ChildItem $dl -Filter *.whl | ForEach-Object { $_.FullName })
        if ($whls.Count -eq 0) { throw "未在 $dl 找到 .whl" }
        & $py -m pip install --no-deps @whls
        if ($LASTEXITCODE -ne 0) { throw "CUDA 版 torch 本地安装失败" }
    }

    Warn "安装完成后请把 config.yaml 的 video_repair.device 改为 cuda"
} else {
    Pip ".venv-rve" @("torch==$TORCH_VER_RVE", "torchvision==$TV_VER_RVE", "numpy==2.2.2")
    Warn "已安装 CPU 版 torch —— config.yaml 的 video_repair.device 应保持 cpu"
}

& ".\.venv-rve\Scripts\python.exe" -c "import torch, cv2, numpy; print('  RVE 环境就绪, torch', torch.__version__, '| numpy', numpy.__version__)"
& ".\.venv-rve\Scripts\python.exe" "tools\backend\rve-backend.py" --list_backends

# --------------------------------------------------------------------------- #
Step "6/6 下载 RVE 模型（含完整性校验）"
New-Item -ItemType Directory -Force -Path "tools\models" | Out-Null

# name -> 期望字节数（用于校验完整性，避免截断）
$MODELS = @(
    @{ Name = "2x_OpenProteus_Compact_i2_70K.pth"; Bytes = 2419483 },   # 2x 通用超分
    @{ Name = "1xDeH264_realplksr.pth";            Bytes = 29559554 }   # 1x 压缩伪影修复
)
$base = "https://github.com/TNTwise/real-video-enhancer-models/releases/download/models/"
foreach ($m in $MODELS) {
    $dst = "tools\models\$($m.Name)"
    if ((Test-Path $dst) -and ((Get-Item $dst).Length -eq $m.Bytes)) { Ok "$($m.Name) 已存在且完整"; continue }
    Write-Host "  下载 $($m.Name) ..."
    $okModel = $false
    for ($i = 1; $i -le 3 -and -not $okModel; $i++) {
        try {
            Invoke-WebRequest -Uri "$base$($m.Name)" -OutFile $dst -UseBasicParsing
            if ((Get-Item $dst).Length -eq $m.Bytes) { $okModel = $true }
            else { Warn "第 $i 次下载不完整（$((Get-Item $dst).Length) / $($m.Bytes) 字节），重试" }
        } catch { Warn "第 $i 次下载失败: $_" }
    }
    if (-not $okModel) { Warn "$($m.Name) 下载不完整，使用时会被适配器跳过（见日志警告）" }
    else { Ok "$($m.Name) 完整" }
}

# --------------------------------------------------------------------------- #
Step "完成 — 运行依赖自检"
& ".\.venv\Scripts\python.exe" main.py doctor

Write-Host @"

后续步骤：
  1) 确认 config.yaml 中 video_repair.device 与实际 torch 版本一致（cpu / cuda）
  2) 把待处理视频放入 input/ 目录（支持子目录）
  3) 扫描并入队：  .\.venv\Scripts\python.exe main.py scan
  4) 无人值守运行：.\.venv\Scripts\python.exe main.py run
  5) 查看状态：    .\.venv\Scripts\python.exe main.py status
  6) 校验修复效果：.\.venv\Scripts\python.exe scripts\verify_repair.py <源> <输出>

注意：main.py 需要 PATH 上有 ffmpeg/ffprobe；
      若未加入 PATH，请把 ffmpeg 目录临时加到 PATH 后再运行。
"@ -ForegroundColor Gray
