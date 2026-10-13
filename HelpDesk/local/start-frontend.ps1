# 启动 HelpDesk 前端（Vite dev server，默认 5173，/api/python 反向代理到 8000）
$Root     = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path (Split-Path $Root -Parent) 'HelpDeskFrontend'

if (-not (Test-Path -LiteralPath $Frontend)) { throw "找不到前端目录：$Frontend" }

Set-Location $Frontend
if (-not (Test-Path -LiteralPath (Join-Path $Frontend 'node_modules'))) {
    Write-Host '未发现 node_modules，先执行 npm install ...' -ForegroundColor Cyan
    npm install
}

Write-Host '启动前端：http://localhost:5173 （Ctrl+C 停止）' -ForegroundColor Cyan
npm run dev
