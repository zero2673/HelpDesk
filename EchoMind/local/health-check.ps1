# 本地服务自检：按项目文档推荐的验证顺序打一遍主要接口
$api = 'http://127.0.0.1:8000'

function Show-Endpoint([string]$Name, [string]$Uri, [string]$Method = 'GET') {
    try {
        $resp = Invoke-RestMethod -Uri $Uri -Method $Method -TimeoutSec 20
        Write-Host "[OK]   $Name  -> $Uri" -ForegroundColor Green
        ($resp | ConvertTo-Json -Depth 5)
    } catch {
        Write-Host "[FAIL] $Name  -> $Uri" -ForegroundColor Red
        Write-Host "       $($_.Exception.Message)" -ForegroundColor DarkRed
    }
    Write-Host ''
}

Show-Endpoint 'health'          "$api/health"
Show-Endpoint 'skills'          "$api/skills"
Show-Endpoint 'knowledge/stats' "$api/knowledge/stats"
Show-Endpoint 'monitor'         "$api/monitor"
