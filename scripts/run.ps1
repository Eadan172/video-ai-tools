# run.ps1 — 一键启动（扫描 + 无人值守运行）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\run.ps1

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "=== 环境检查 ===" -ForegroundColor Cyan
python main.py doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "[警告] 部分依赖缺失，流水线将以可用功能继续（见上方提示）" -ForegroundColor Yellow
}

Write-Host "=== 扫描 input/ ===" -ForegroundColor Cyan
python main.py scan

Write-Host "=== 开始无人值守处理（Ctrl+C 可安全中断，重启本脚本即可续跑）===" -ForegroundColor Cyan
python main.py run
