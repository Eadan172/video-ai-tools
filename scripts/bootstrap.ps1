# =====================================================================
#  视频批量修复流水线 —— 一键运行引导脚本（由 run.bat 双击调用）
#
#  职责：
#    1. 定位/创建虚拟环境并安装依赖
#    2. 定位 FFmpeg（仓库内 .tools\ffmpeg → 上级目录 → PATH → 自动下载）
#    3. 检查 AI 修复组件（RVE / DeepFilterNet），缺失时自动降级为纯转码
#    4. 交互式选择输出格式与处理范围（可用参数跳过）
#    5. 执行 scan + run，失败任务由流水线自行隔离到 failed\
#
#  注意：本脚本刻意不使用 param() 块 —— 这样 run.bat 传来的 "--format mkv"
#        这类参数会原样进入 $args，由下面的手动解析器处理（兼容 -x / --x / --x=y）。
# =====================================================================

$ErrorActionPreference = "Stop"
$RawArgs = @($args)
$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

# --------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------- #
$Format = ""
$Only = ""
$Tier = ""
$ConfigFile = "config.yaml"
$DryRun = $false
$SelfTest = $false
$NoPrompt = $false
$NoAI = $false
$Unknown = @()

$i = 0
while ($i -lt $RawArgs.Count) {
    $tok = [string]$RawArgs[$i]
    $key = $tok
    $val = ""
    if ($tok -match '^-{1,2}([^=]+)=(.*)$') {
        $key = $Matches[1]
        $val = $Matches[2]
    } else {
        $key = $tok.TrimStart('-')
    }
    $key = $key.ToLower()

    switch ($key) {
        "format"    { if (-not $val) { $i++; $val = [string]$RawArgs[$i] }; $Format = $val }
        "only"      { if (-not $val) { $i++; $val = [string]$RawArgs[$i] }; $Only = $val }
        "tier"      { if (-not $val) { $i++; $val = [string]$RawArgs[$i] }; $Tier = $val }
        "config"    { if (-not $val) { $i++; $val = [string]$RawArgs[$i] }; $ConfigFile = $val }
        "dry-run"   { $DryRun = $true }
        "dryrun"    { $DryRun = $true }
        "selftest"  { $SelfTest = $true }
        "self-test" { $SelfTest = $true }
        "no-prompt" { $NoPrompt = $true }
        "noprompt"  { $NoPrompt = $true }
        "no-ai"     { $NoAI = $true }
        "noai"      { $NoAI = $true }
        default     { $Unknown += $tok }
    }
    $i++
}

# --------------------------------------------------------------------- #
# 输出助手
# --------------------------------------------------------------------- #
$LogFile = Join-Path $Root "logs\launcher.log"
function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    try {
        $dir = Split-Path -Parent $LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch { }
}
function Step([string]$msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan; Write-Log "== $msg" }
function Ok([string]$msg)   { Write-Host "   [OK]   $msg" -ForegroundColor Green;  Write-Log "OK $msg" }
function Warn([string]$msg) { Write-Host "   [注意] $msg" -ForegroundColor Yellow; Write-Log "WARN $msg" }
function Fail([string]$msg) { Write-Host "   [错误] $msg" -ForegroundColor Red;    Write-Log "ERR $msg" }

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host "   视频批量修复流水线  ·  一键运行" -ForegroundColor Cyan
Write-Host "   （画质 AI 修复 + 音质 AI 降噪 + 转格式导出）" -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Log "launcher start; args=$($RawArgs -join ' ')"

if ($Unknown.Count -gt 0) { Warn "忽略无法识别的参数：$($Unknown -join ' ')" }

# --------------------------------------------------------------------- #
# 1. 定位 Python
# --------------------------------------------------------------------- #
Step "1/6  检查 Python"
$PyExe = ""
$PyPre = @()
foreach ($cand in @(@("py", "-3.11"), @("py", "-3"), @("python"))) {
    $exe = $cand[0]
    $pre = @($cand | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $out = & $exe @pre -c "import sys;print(sys.version_info[0], sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $p = ([string]$out).Trim() -split '\s+'
            if ([int]$p[0] -eq 3 -and [int]$p[1] -ge 11) {
                $PyExe = $exe; $PyPre = $pre
                Ok ("Python {0}.{1}  ({2} {3})" -f $p[0], $p[1], $exe, ($pre -join ' '))
                break
            } else {
                Warn ("忽略 Python {0}.{1}（需要 >= 3.11）" -f $p[0], $p[1])
            }
        }
    } catch { }
}
if (-not $PyExe) {
    Fail "未找到 Python >= 3.11。请先安装 Python 3.11+ 并勾选 Add to PATH，然后重试。"
    Write-Log "abort: no python"
    exit 1
}

# --------------------------------------------------------------------- #
# 2. 虚拟环境与依赖
# --------------------------------------------------------------------- #
Step "2/6  准备虚拟环境 .venv"
$VenvDir = Join-Path $Root ".venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    # 兼容旧布局：虚拟环境建在上级目录的情况
    $legacy = Join-Path (Split-Path -Parent $Root) ".venv\Scripts\python.exe"
    if (Test-Path $legacy) {
        $VenvPy = $legacy
        $VenvDir = Split-Path -Parent (Split-Path -Parent $legacy)
        Warn "使用上级目录已有的虚拟环境：$VenvDir"
    }
}
if (-not (Test-Path $VenvPy)) {
    Write-Host "   创建虚拟环境（首次运行，约十几秒）..."
    $ErrorActionPreference = "Continue"
    & $PyExe @PyPre -m venv $VenvDir
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rc -ne 0 -or -not (Test-Path $VenvPy)) {
        Fail "虚拟环境创建失败（退出码 $rc）。"
        exit 1
    }
    Ok "已创建 $VenvDir"
} else {
    Ok "已存在 $VenvPy"
}

$NeedInstall = $false
$ErrorActionPreference = "Continue"
& $VenvPy -c "import yaml, psutil" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { $NeedInstall = $true }
$ErrorActionPreference = "Stop"

if ($NeedInstall) {
    Write-Host "   安装 requirements.txt（首次运行）..."
    $req = Join-Path $Root "requirements.txt"
    if (Test-Path $req) {
        $ErrorActionPreference = "Continue"
        & $VenvPy -m pip install --disable-pip-version-check -q -r $req
        $rc = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($rc -ne 0) { Fail "依赖安装失败（退出码 $rc）。请检查网络后重试。"; exit 1 }
    }
}
$ErrorActionPreference = "Continue"
& $VenvPy -c "import yaml, psutil" 2>$null | Out-Null
$depsOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $depsOk) { Fail "依赖校验未通过（PyYAML / psutil 无法导入）。"; exit 1 }
Ok "依赖就绪"

# --------------------------------------------------------------------- #
# 3. 定位 FFmpeg
# --------------------------------------------------------------------- #
Step "3/6  检查 FFmpeg / FFprobe"
function Test-FFmpegDir([string]$dir) {
    if (-not $dir) { return $false }
    return ((Test-Path (Join-Path $dir "ffmpeg.exe")) -and (Test-Path (Join-Path $dir "ffprobe.exe")))
}
$FfDir = ""
foreach ($cand in @(
        (Join-Path $Root ".tools\ffmpeg"),
        (Join-Path (Split-Path -Parent $Root) ".tools\ffmpeg"))) {
    if (Test-FFmpegDir $cand) { $FfDir = $cand; break }
}
if (-not $FfDir) {
    $onPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($onPath) {
        $FfDir = Split-Path -Parent $onPath.Source
        Warn "使用 PATH 上的 FFmpeg：$FfDir"
    }
}
if (-not $FfDir) {
    Write-Host "   未找到 FFmpeg，尝试自动下载（约 30MB，来自 npmmirror 镜像）..."
    $dest = Join-Path $Root ".tools\ffmpeg"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $base = "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/"
    $pairs = @(@("ffmpeg-win32-x64.gz", "ffmpeg.exe"), @("ffprobe-win32-x64.gz", "ffprobe.exe"))
    $okAll = $true
    foreach ($pair in $pairs) {
        $url = $base + $pair[0]
        $exe = Join-Path $dest $pair[1]
        try {
            $gz = "$exe.gz"
            Invoke-WebRequest -Uri $url -OutFile $gz -UseBasicParsing
            $in = [IO.File]::OpenRead($gz)
            $out = [IO.File]::Create($exe)
            $gzs = New-Object IO.Compression.GZipStream($in, [IO.Compression.CompressionMode]::Decompress)
            $gzs.CopyTo($out)
            $gzs.Dispose(); $out.Dispose(); $in.Dispose()
            Remove-Item $gz -Force -ErrorAction SilentlyContinue
            Ok "已下载 $($pair[1])"
        } catch {
            $okAll = $false
            Fail "下载 $($pair[1]) 失败：$($_.Exception.Message)"
        }
    }
    if ($okAll -and (Test-FFmpegDir $dest)) { $FfDir = $dest }
}
if (-not $FfDir) {
    Fail "FFmpeg 不可用。请手动安装后二选一："
    Write-Host "      a) 把 ffmpeg.exe / ffprobe.exe 放到  $Root\.tools\ffmpeg\"
    Write-Host "      b) 安装到系统 PATH（winget install Gyan.FFmpeg）"
    exit 1
}
$env:PATH = "$FfDir;$env:PATH"
Ok "FFmpeg 目录：$FfDir"

# --------------------------------------------------------------------- #
# 4. AI 组件检查
# --------------------------------------------------------------------- #
Step "4/6  检查 AI 修复组件"
$MainPy = Join-Path $Root "main.py"
if (-not (Test-Path $MainPy)) { Fail "未找到 main.py，当前目录不是流水线根目录。"; exit 1 }

$doctorOut = @()
$ErrorActionPreference = "Continue"
$doctorOut = & $VenvPy $MainPy --config=$ConfigFile doctor 2>&1 | ForEach-Object { [string]$_ }
$doctorRc = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$doctorText = ($doctorOut -join "`n")

if ($doctorRc -eq 0) {
    Ok "全部依赖就绪（含 AI 修复组件）"
    Write-Log "doctor ok"
} else {
    Warn "环境自检未全部通过："
    $doctorOut | ForEach-Object { Write-Host "      $_" }
    # AI 组件缺失时若不降级，每个任务都会在 REPAIR_VIDEO 直接 FAILED_FINAL
    if (-not $NoAI) {
        $NoAI = $true
        Warn "已自动切换为「纯转码模式」（--no-ai）：跳过 AI 修复，只做转格式导出。"
        Warn "如需 AI 修复，请先运行： scripts\setup_ai_tools.ps1 -Cuda"
    }
}

# --------------------------------------------------------------------- #
# 5. 交互式选项
# --------------------------------------------------------------------- #
$Formats = @(
    @("mp4",  "MP4  · H.265 + AAC   （默认，兼容性最好）"),
    @("mkv",  "MKV  · H.265 + AAC   （无损容器，支持多音轨）"),
    @("mov",  "MOV  · H.265 + AAC   （苹果生态）"),
    @("webm", "WEBM · VP9 + Opus    （网页播放）"),
    @("avi",  "AVI  · H.264 + MP3   （老设备兼容）")
)

Step "5/6  处理选项"

# ---- 修复档位（用户可选三档 + 自动）----
$Tiers = @(
    @("light",    "light     仅转码（最快，不做 AI）                  约 5 分钟/集"),
    @("standard", "standard  中档：2x AI 超分 + 音质降噪            约 1.5 小时/集"),
    @("full",     "full      完全修复：1x 压缩修复 + 2x 超分 + 音质    约 14.8 小时/集")
)
if (-not $Tier) {
    if ($NoPrompt -or $DryRun -or $SelfTest) {
        $Tier = "auto"
    } else {
        Write-Host "   请选择修复档位："
        for ($n = 0; $n -lt $Tiers.Count; $n++) {
            Write-Host ("      [{0}] {1}" -f ($n + 1), $Tiers[$n][1])
        }
        Write-Host "      [0] auto      自动（默认，按片源与队列预算在中档/完全修复间自动选）"
        $sel = Read-Host "   输入序号后回车（直接回车 = auto）"
        $sel = ([string]$sel).Trim()
        if ($sel -match '^\d+$' -and [int]$sel -ge 1 -and [int]$sel -le $Tiers.Count) {
            $Tier = $Tiers[[int]$sel - 1][0]
        } else {
            $Tier = "auto"
        }
    }
}
if (@("auto", "light", "standard", "full") -notcontains $Tier.ToLower()) {
    Warn "未知档位 '$Tier'，回退到 auto"
    $Tier = "auto"
}
$Tier = $Tier.ToLower()
if ($NoAI -and $Tier -ne "light") {
    Warn "AI 组件不可用：档位由 $Tier 强制改为 light（仅转码），否则每个任务都会失败"
    $Tier = "light"
}
Ok "修复档位：$Tier"

if ($DryRun -or $SelfTest) {
    if (-not $Format) { $Format = "mp4" }
    Warn "本次为 --dry-run / --selftest，跳过交互选择（按 格式=$Format 演示）"
} elseif (-not $Format) {
    if ($NoPrompt) {
        $Format = "mp4"
    } else {
        Write-Host "   请选择输出格式："
        for ($n = 0; $n -lt $Formats.Count; $n++) {
            Write-Host ("      [{0}] {1}" -f ($n + 1), $Formats[$n][1])
        }
        $sel = Read-Host "   输入序号后回车（直接回车 = mp4）"
        $sel = ([string]$sel).Trim()
        if ($sel -match '^\d+$' -and [int]$sel -ge 1 -and [int]$sel -le $Formats.Count) {
            $Format = $Formats[[int]$sel - 1][0]
        } else {
            $Format = "mp4"
        }
    }
}
if ($Formats | ForEach-Object { $_[0] } | Where-Object { $_ -eq $Format.ToLower() }) {
    $Format = $Format.ToLower()
} else {
    Warn "未知格式 '$Format'，回退到 mp4"
    $Format = "mp4"
}
Ok "输出格式：$Format"

if (-not $DryRun -and -not $SelfTest) {
    if (-not $Only -and -not $NoPrompt) {
        $subs = @()
        $inDir = Join-Path $Root "input"
        if (Test-Path $inDir) {
            $subs = @(Get-ChildItem $inDir -Directory -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -notlike "._*" } | ForEach-Object { $_.Name })
        }
        if ($subs.Count -gt 1) {
            Write-Host "   检测到多个输入子目录："
            for ($n = 0; $n -lt $subs.Count; $n++) {
                Write-Host ("      [{0}] {1}" -f ($n + 1), $subs[$n])
            }
            $sel = Read-Host "   只处理哪一个？（直接回车 = 全部处理）"
            $sel = ([string]$sel).Trim()
            if ($sel -match '^\d+$' -and [int]$sel -ge 1 -and [int]$sel -le $subs.Count) {
                $Only = $subs[[int]$sel - 1]
            }
        }
    }
    if ($Only) { Ok "处理范围：input\$Only" } else { Ok "处理范围：input\ 全部子目录" }
}

# --------------------------------------------------------------------- #
# 6. 执行
# --------------------------------------------------------------------- #
$commonArgs = @("--config=$ConfigFile", "--format=$Format", "--tier=$Tier")
if ($Only) { $commonArgs += "--only=$Only" }

if ($DryRun) {
    Step "6/6  环境检查完成（--dry-run，不执行处理）"
    Ok "Python      : $VenvPy"
    Ok "FFmpeg      : $FfDir"
    Ok "输出格式    : $Format"
    Ok "修复档位    : $Tier"
    if ($Only) { Ok "处理范围    : input\$Only" } else { Ok "处理范围    : input\ 全部" }
    Write-Host ""
    Write-Host "   将执行：" -ForegroundColor Cyan
    Write-Host ("     {0} main.py {1} scan" -f $VenvPy, ($commonArgs -join ' '))
    Write-Host ("     {0} main.py {1} run" -f $VenvPy, ($commonArgs -join ' '))
    Write-Log "dry-run exit 0"
    exit 0
}

if ($SelfTest) {
    Step "6/6  自测（pytest）"
    $ErrorActionPreference = "Continue"
    & $VenvPy -m pytest (Join-Path $Root "tests") -q --no-header
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rc -ne 0) { Fail "自测未通过（退出码 $rc）"; exit $rc }
    Ok "自测通过"
    exit 0
}

Step "6/6  开始处理"
Write-Host "   （AI 修复耗时较长：整集 1x+2x 约十几小时；可随时 Ctrl+C 安全中断，"
Write-Host "     再次运行会从断点继续，已完成的阶段不会重跑）"
Write-Log "scan: $($commonArgs -join ' ')"
$ErrorActionPreference = "Continue"
& $VenvPy $MainPy @commonArgs scan
$scanRc = $LASTEXITCODE
Write-Log "scan rc=$scanRc"

Write-Log "run: $($commonArgs -join ' ')"
& $VenvPy $MainPy @commonArgs run
$runRc = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Write-Log "run rc=$runRc"

Write-Host ""
Write-Host "   最近状态：" -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
& $VenvPy $MainPy @commonArgs status 2>&1 | ForEach-Object { Write-Host "      $_" }
$ErrorActionPreference = "Stop"

if ($runRc -ne 0) { Fail "run 退出码 $runRc"; exit $runRc }
Write-Log "launcher exit 0"
exit 0