# 一键启动点餐服务（从「用户环境变量」读取凭据，不含明文密钥）
# 用法：右键「使用 PowerShell 运行」，或在本目录执行  .\start.ps1
# 首次使用先配置（只需一次）：
#   setx MCD_TOKEN "你的麦当劳 token"
#   setx MINIMAX_API_KEY "你的 MiniMax key"
#   然后关掉终端重开，让新变量生效。

$ErrorActionPreference = "Stop"

$env:MCD_TOKEN = [Environment]::GetEnvironmentVariable("MCD_TOKEN", "User")
$env:MINIMAX_API_KEY = [Environment]::GetEnvironmentVariable("MINIMAX_API_KEY", "User")

if (-not $env:MCD_TOKEN) {
    Write-Host "缺少 MCD_TOKEN。请先执行： setx MCD_TOKEN `"你的麦当劳 token`"  然后重开终端。" -ForegroundColor Red
    exit 1
}

Set-Location $PSScriptRoot

$port = 8000
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "端口 $port 已被占用（PID $($busy.OwningProcess -join ',')）。" -ForegroundColor Yellow
    Write-Host "可能是上一次的服务还在跑：先关掉它，或执行任务管理器结束该进程。" -ForegroundColor Yellow
    exit 1
}

if ($env:MINIMAX_API_KEY) {
    Write-Host "智能解析：已启用（MiniMax）" -ForegroundColor Green
} else {
    Write-Host "智能解析：未启用（没有 MINIMAX_API_KEY，配餐会退回关键词解析）" -ForegroundColor Yellow
}
Write-Host "地址：http://127.0.0.1:$port" -ForegroundColor Green

python app\server.py
