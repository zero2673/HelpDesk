# HelpDesk 本地一键启动（Windows 原生运行，不需要 Docker）
# Redis 用的是 .env 里 REDIS_URL 指向的实例（当前是你虚拟机 192.168.100.128 上的 Redis）。
# 启动顺序：检查 Redis -> ChromaDB(8001) -> HelpDesk API(8000，前台运行)
#
# 用法：
#   .\local\start-all.ps1                      启动 HTTP 服务（Swagger: http://localhost:8000/docs）
#   .\local\start-all.ps1 -Cli                 启动命令行交互模式
#   .\local\start-all.ps1 -LocalRedisFallback  虚拟机 Redis 连不上时，用本机自带的 Redis 兜底
param([switch]$Cli, [switch]$LocalRedisFallback)

$ErrorActionPreference = 'Stop'

# 中文 Windows 默认控制台是 GBK(936)；横幅已改成纯 ASCII，这里仍设置编码，防其他中文输出出问题。
# 必须在启动 Python 之前设置这两个变量（.env 里设置已经太晚，解释器启动时就决定了编码）。
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root          = Split-Path -Parent $PSScriptRoot
$ToolsRoot     = $env:HELPDESK_TOOLS_DIR
if (-not $ToolsRoot) { $ToolsRoot = Join-Path (Split-Path $Root -Parent) 'tools' }   # 默认用项目上级目录的 tools\redis，可用 HELPDESK_TOOLS_DIR 覆盖
$RedisDir      = Join-Path $ToolsRoot 'redis'
$RedisExe      = Join-Path $RedisDir 'redis-server.exe'
$Python        = Join-Path $Root '.venv-win\Scripts\python.exe'
$ChromaExe     = Join-Path $Root '.venv-win\Scripts\chroma.exe'
$LogDir        = Join-Path $Root 'logs'
$ChromaDataDir = Join-Path $Root 'data\chroma'
$LocalRedisUrl = 'redis://:helpdesk123@127.0.0.1:6379/1'

foreach ($exe in @($Python, $ChromaExe)) {
    if (-not (Test-Path -LiteralPath $exe)) { throw "缺少可执行文件：$exe" }
}
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

# ── 0. 解析 .env 里的 REDIS_URL，确定要连哪个 Redis ─────────────────────────
$redisTarget = '127.0.0.1'
$redisPort   = 6379
$envFile     = Join-Path $Root '.env'
if (Test-Path -LiteralPath $envFile) {
    $hit = Select-String -LiteralPath $envFile -Pattern '^\s*REDIS_URL\s*=\s*(\S+)\s*$' | Select-Object -First 1
    if ($hit) {
        $redisUrl = $hit.Matches[0].Groups[1].Value
        if ($redisUrl -match '@([^:@/]+):(\d+)')          { $redisTarget = $matches[1]; $redisPort = [int]$matches[2] }
        elseif ($redisUrl -match '://([^:@/]+):(\d+)')    { $redisTarget = $matches[1]; $redisPort = [int]$matches[2] }
    }
}

# ── 1. Redis（工作记忆）──────────────────────────────────────────────────────
if (Test-Port $redisTarget $redisPort) {
    Write-Host "[1/3] Redis 可用：${redisTarget}:${redisPort}（来自 .env 的 REDIS_URL），跳过本机 Redis" -ForegroundColor DarkGray
} elseif ($LocalRedisFallback) {
    Write-Host '[1/3] 目标 Redis 连不上，改用本机自带的 Redis（6379）兜底...' -ForegroundColor Yellow
    if (-not (Test-Path -LiteralPath $RedisExe)) { throw "缺少可执行文件：$RedisExe" }
    $redisConf = Join-Path $RedisDir 'redis.windows.conf'
    Start-Process -FilePath $RedisExe `
        -ArgumentList @($redisConf, '--requirepass', 'helpdesk123', '--appendonly', 'yes') `
        -WorkingDirectory $RedisDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'redis.out.log') `
        -RedirectStandardError  (Join-Path $LogDir 'redis.err.log')
    if (-not (Wait-Port '127.0.0.1' 6379 30)) { throw '本机 Redis 启动失败，请看 logs\redis.err.log' }
    # 覆盖子进程环境变量：load_dotenv 不会覆盖已存在的环境变量，所以应用会连本机 Redis
    $env:REDIS_URL = $LocalRedisUrl
    Write-Host '      注意：本次运行使用的是本机 Redis，不是虚拟机里的那个' -ForegroundColor Yellow
} else {
    Write-Host "[1/3] 连不上 Redis ${redisTarget}:${redisPort}" -ForegroundColor Red
    Write-Host '      如果用的是虚拟机上的 Redis：先确认虚拟机已开机、里面的 redis-server 在运行、' -ForegroundColor Yellow
    Write-Host '      并且桥接/仅主机网络能通（当前走 VMware VMnet1 网段）。' -ForegroundColor Yellow
    Write-Host '      想临时用本机自带的 Redis 兜底： .\local\start-all.ps1 -LocalRedisFallback' -ForegroundColor Yellow
    exit 1
}

# ── 2. ChromaDB（情景记忆 + 用户画像 + 知识库向量）──────────────────────────
if (Test-Port '127.0.0.1' 8001) {
    Write-Host '[2/3] ChromaDB 已在 8001 运行，跳过' -ForegroundColor DarkGray
} else {
    Write-Host '[2/3] 启动 ChromaDB (8001)...' -ForegroundColor Cyan
    Start-Process -FilePath $ChromaExe `
        -ArgumentList @('run', '--path', $ChromaDataDir, '--host', '127.0.0.1', '--port', '8001', '--log-path', (Join-Path $LogDir 'chroma.log')) `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'chroma.out.log') `
        -RedirectStandardError  (Join-Path $LogDir 'chroma.err.log')
    if (-not (Wait-Port '127.0.0.1' 8001 120)) { throw 'ChromaDB 启动失败，请看 logs\chroma.err.log' }
}

# ── 3. HelpDesk 主程序（前台运行）───────────────────────────────────────────
Set-Location $Root
if ($Cli) {
    Write-Host '[3/3] 启动 HelpDesk CLI（输入 quit 退出）' -ForegroundColor Cyan
    & $Python (Join-Path $Root 'api\main.py') --cli
} else {
    if (Test-Port '127.0.0.1' 8000) {
        Write-Host '端口 8000 已被占用：可能已经有一个 HelpDesk 实例在跑。' -ForegroundColor Yellow
        Write-Host '如需重启，请先执行： .\local\stop-all.ps1' -ForegroundColor Yellow
        exit 1
    }
    Write-Host '[3/3] 启动 HelpDesk API (8000)，按 Ctrl+C 停止' -ForegroundColor Cyan
    Write-Host '      Swagger: http://localhost:8000/docs' -ForegroundColor DarkGray
    & $Python (Join-Path $Root 'api\main.py')
}
