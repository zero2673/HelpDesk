# 停止本地运行的 HelpDesk 相关进程
#
# 注意：.env 里 APP_ENV=development 时，API 是 uvicorn 热重载模式，会存在
#   "reloader 父进程 + 实际服务的子进程" 两个 python 进程。
# 只按端口杀掉那个监听者，reloader 父进程会立刻把子进程重新拉起来，
# 而且子进程沿用父进程的旧环境变量（改了 .env 也不生效）。
# 所以这里用"按命令行找进程树 + 按端口兜底"两种方式一起清理。

$Root = Split-Path -Parent $PSScriptRoot
$stopped = 0

# ── 1. 按命令行找出本项目的 python 进程树 ───────────────────────────────────
$allPython = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue

$mine = @($allPython | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -like "*$Root*" -or
        $_.CommandLine -match 'api[\\/]main\.py'
    )
})

# 热重载的子进程命令行里没有项目路径（是 multiprocessing.spawn），按父进程找出来
$children = @($allPython | Where-Object { $mine.ProcessId -contains $_.ParentProcessId })

# 子进程比父进程新，先杀子再杀父
$targets = @($children + $mine) | Sort-Object CreationDate -Descending | Sort-Object ProcessId -Unique

foreach ($t in $targets) {
    $cmd = if ($t.CommandLine) { $t.CommandLine.Substring(0, [Math]::Min(90, $t.CommandLine.Length)) } else { '' }
    Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host ("停止 python PID {0}  {1}" -f $t.ProcessId, $cmd) -ForegroundColor Yellow
    $stopped++
}

# ── 2. 端口兜底（API / ChromaDB / 本机 Redis / 前端 node）────────────────────
foreach ($port in 8000, 8001, 6379, 5173) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($procId in ($conns.OwningProcess | Sort-Object -Unique)) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host ("停止 {0} (PID {1}, 端口 {2})" -f $proc.ProcessName, $procId, $port) -ForegroundColor Yellow
            $stopped++
        }
    }
}

Start-Sleep -Milliseconds 800

$still = @()
foreach ($port in 8000, 8001, 6379, 5173) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { $still += $port }
}
if ($still.Count -gt 0) {
    Write-Host "注意：端口 $($still -join ', ') 仍在监听，可以再执行一次本脚本" -ForegroundColor Red
} else {
    Write-Host "已停止 $stopped 个进程，8000 / 8001 / 6379 / 5173 均已释放" -ForegroundColor Cyan
}
