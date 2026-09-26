# doctor.ps1 — 环境依赖检查
# 用法：powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
python main.py doctor
