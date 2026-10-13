# 只启动依赖服务（检查 Redis + 启动 ChromaDB），不启动 API。
# 用途：在 PyCharm 里 Run/Debug "HelpDesk API" 时，先用这个把依赖拉起来，
#       避免和 start-all.ps1 抢 8000 端口。
#
# 用法：
#   .\local\start-deps.ps1
#   .\local\start-deps.ps1 -LocalRedisFallback   虚拟机 Redis 连不上时用本机兜底
param([switch]$LocalRedisFallback)

$ErrorActionPreference = 'Stop'

$Root          = Split-Path -Parent $PSScriptRoot
$ToolsRoot     = $env:HELPDESK_TOOLS_DIR
if (-not $ToolsRoot) { $ToolsRoot = Join-Path (Split-Path $Root -Parent) 'tools' }   # 默认用项目上级目录的 tools\redis，可用 HELPDESK_TOOLS_DIR 覆盖
$RedisDir      = Join-Path $ToolsRoot 'redis'
$RedisExe      = Join-Path $RedisDir 'redis-server.exe'
$ChromaExe     = Join-Path $Root '.venv-win\Scripts\chroma.exe'
$LogDir        = Join-Path $Root 'logs'
$ChromaDataDir = Join-Path $Root 'data\chroma'
$LocalRedisUrl = 'redis://:helpdesk123@127.0.0.1:6379/1'

if (-not (Test-Path -LiteralPath $ChromaExe)) { throw "缺少可执行文件：$ChromaExe" }
New-Item -ItemType Directory -Force -Path $LogDir, $ChromaDataDir | Out-Null

function Test-Port([string]$Target, [int]$Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync($Target, $Port)
        $ok = $task.Wait(3000) -and $client.Connected
        $client.Close()
        return $ok
    } catch { return $false }
}

function Wait-Port([string]$Target, [int]$Port, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Target $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# ── 0. 从 .env 解析 REDIS_URL ──────────────────────────────────────────────
$redisTarget = '127.0.0.1'
$redisPort   = 6379
$envFile     = Join-Path $Root '.env'
if (Test-Path -LiteralPath $envFile) {
    $hit = Select-String -LiteralPath $envFile -Pattern '^\s*REDIS_URL\s*=\s*(\S+)\s*$' | Select-Object -First 1
    if ($hit) {
        $redisUrl = $hit.Matches[0].Groups[1].Value
        if ($redisUrl -match '@([^:@/]+):(\d+)')       { $redisTarget = $matches[1]; $redisPort = [int]$matches[2] }
        elseif ($redisUrl -match '://([^:@/]+):(\d+)') { $redisTarget = $matches[1]; $redisPort = [int]$matches[2] }
    }
}

# ── 1. Redis ───────────────────────────────────────────────────────────────
if (Test-Port $redisTarget $redisPort) {
    Write-Host "[1/2] Redis 可用：${redisTarget}:${redisPort}，跳过本机 Redis" -ForegroundColor DarkGray
} elseif ($LocalRedisFallback) {
    Write-Host '[1/2] 目标 Redis 连不上，改用本机自带的 Redis（6379）兜底...' -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $RedisExe)) { throw "缺少可执行文件：$RedisExe" }
    $redisConf = Join-Path $RedisDir 'redis.windows.conf'
    Start-Process -FilePath $RedisExe `
        -ArgumentList @($redisConf, '--requirepass', 'helpdesk123', '--appendonly', 'yes') `
        -WorkingDirectory $RedisDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'redis.out.log') `
        -RedirectStandardError  (Join-Path $LogDir 'redis.err.log')
    if (-not (Wait-Port '127.0.0.1' 6379 30)) { throw '本机 Redis 启动失败，请看 logs\redis.err.log' }
    Write-Host "      注意：本机兜底 Redis 用的是 db1 + 密码 helpdesk123。" -ForegroundColor Yellow
    Write-Host "      请在 PyCharm 运行配置的 Environment variables 里临时加上 REDIS_URL=$LocalRedisUrl" -ForegroundColor Yellow
} else {
    Write-Host "[1/2] 连不上 Redis ${redisTarget}:${redisPort}" -ForegroundColor Red
    Write-Host '      先确认虚拟机已开机、里面的 redis-server 在运行。' -ForegroundColor Yellow
    Write-Host '      想临时用本机自带的 Redis 兜底： .\local\start-deps.ps1 -LocalRedisFallback' -ForegroundColor Yellow
    exit 1
}

# ── 2. ChromaDB ────────────────────────────────────────────────────────────
if (Test-Port '127.0.0.1' 8001) {
    Write-Host '[2/2] ChromaDB 已在 8001 运行，跳过' -ForegroundColor DarkGray
} else {
    Write-Host '[2/2] 启动 ChromaDB (8001)...' -ForegroundColor Cyan
    Start-Process -FilePath $ChromaExe `
        -ArgumentList @('run', '--path', $ChromaDataDir, '--host', '127.0.0.1', '--port', '8001', '--log-path', (Join-Path $LogDir 'chroma.log')) `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'chroma.out.log') `
        -RedirectStandardError  (Join-Path $LogDir 'chroma.err.log')
    if (-not (Wait-Port '127.0.0.1' 8001 120)) { throw 'ChromaDB 启动失败，请看 logs\chroma.err.log' }
}

Write-Host ''
Write-Host '依赖已就绪：现在回 PyCharm 点 Run/Debug "HelpDesk API" 即可（8000 端口留给了它）。' -ForegroundColor Green
Write-Host '停止依赖：.\local\stop-all.ps1' -ForegroundColor DarkGray
