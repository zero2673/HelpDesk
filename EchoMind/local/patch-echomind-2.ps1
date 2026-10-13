# HelpDesk 补丁 2/2：辅助调用统一关闭推理（thinking disabled）
#
# 背景：服务商支持 thinking={"type":"disabled"}。
#   实测同一 prompt：带推理 7.0s/1024 tokens 且文本为空；关推理 1.0s/43 tokens 返回完整 JSON。
#   连主模型 v4-pro 关推理后也只要 0.9s。
#
# 做法：在 core/llm_utils.py 增加 create_helper_message() 统一入口，
#       把 6 处辅助调用（意图 / 改写 / 重排 / Judge / 摘要 / 画像）换成它调用。
#       服务商不支持该参数时自动去掉重试，并在日志里提示一次。
#
# 前置：先跑 patch-helpdesk.ps1
# 用法（项目根目录）：powershell -ExecutionPolicy Bypass -File .\local\patch-helpdesk-2.ps1

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$Backup  = Join-Path $Root 'local\backup-before-patch'
$enc     = New-Object System.Text.UTF8Encoding($false)
$changed = 0
$skipped = 0

function Apply-Changes {
    param([string]$RelPath, [object[]]$Changes)

    $path = Join-Path $Root $RelPath
    if (-not (Test-Path -LiteralPath $path)) { throw "文件不存在：$path" }

    $bkPath = Join-Path $Backup $RelPath
    if (-not (Test-Path -LiteralPath $bkPath)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $bkPath -Parent) | Out-Null
        Copy-Item -LiteralPath $path -Destination $bkPath -Force
    }

    $text  = [System.IO.File]::ReadAllText($path, $enc)
    $nl    = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $dirty = $false

    foreach ($c in $Changes) {
        $old = ($c.Old -join $nl)
        $new = ($c.New -join $nl)
        $count = ([regex]::Matches($text, [regex]::Escape($old))).Count
        if ($count -eq 1) {
            $text  = $text.Replace($old, $new)
            $dirty = $true
        } elseif ($count -eq 0) {
            if ($text.Contains($c.New[0])) { $script:skipped++; continue }
            throw "$RelPath 匹配失败（期望 1 处，实际 0 处），未修改。锚点：$($c.Old[0])"
        } else {
            throw "$RelPath 匹配到 $count 处（期望 1 处），锚点：$($c.Old[0])"
        }
    }

    if ($dirty) {
        [System.IO.File]::WriteAllText($path, $text, $enc)
        Write-Host "  已修改 $RelPath" -ForegroundColor Green
        $script:changed++
    } else {
        Write-Host "  跳过 $RelPath（已是补丁后状态）" -ForegroundColor DarkGray
    }
}

Write-Host "开始打补丁 2/2，备份目录：$Backup" -ForegroundColor Cyan

# ── core/llm_utils.py：加 logging + 统一辅助调用入口 ─────────────────────────
Apply-Changes 'core\llm_utils.py' @(
    @{
        Old = @(
            'import json',
            'import os',
            'import re',
            'from typing import Any, Iterable, List'
        )
        New = @(
            'import json',
            'import logging',
            'import os',
            'import re',
            'from typing import Any, Dict, Iterable, List, Optional',
            '',
            'logger = logging.getLogger(__name__)'
        )
    }
    @{
        Old = @(
            'def parse_json_response(raw: str, open_char: str = "{", close_char: str = "}") -> Any:',
            '    """取出并解析 JSON；结构异常时抛出带原因的异常，由调用方决定兜底策略。"""',
            '    return json.loads(extract_json_payload(raw, open_char, close_char))'
        )
        New = @(
            'def parse_json_response(raw: str, open_char: str = "{", close_char: str = "}") -> Any:',
            '    """取出并解析 JSON；结构异常时抛出带原因的异常，由调用方决定兜底策略。"""',
            '    return json.loads(extract_json_payload(raw, open_char, close_char))',
            '',
            '',
            '# 服务商是否支持 thinking={"type": "disabled"}，None 表示还没探测过',
            '_THINKING_DISABLED_SUPPORTED: Optional[bool] = None',
            '',
            '',
            'def thinking_disabled_by_config() -> bool:',
            '    """HELPDESK_DISABLE_THINKING=0/false/no/off 时保留推理，其余情况关闭推理。"""',
            '    flag = (os.getenv("HELPDESK_DISABLE_THINKING", "1") or "").strip().lower()',
            '    return flag not in ("0", "false", "no", "off")',
            '',
            '',
            'def _thinking_extra_body() -> Optional[Dict[str, Any]]:',
            '    global _THINKING_DISABLED_SUPPORTED',
            '    if _THINKING_DISABLED_SUPPORTED is False:',
            '        return None',
            '    if not thinking_disabled_by_config():',
            '        return None',
            '    return {"thinking": {"type": "disabled"}}',
            '',
            '',
            'async def create_helper_message(',
            '    client: Any,',
            '    *,',
            '    model: str,',
            '    prompt: str,',
            '    temperature: float = 0.0,',
            '    max_tokens: Optional[int] = None,',
            ') -> Any:',
            '    """',
            '    辅助调用（意图 / 改写 / 重排 / Judge / 摘要 / 画像）统一入口。',
            '',
            '    推理模型的 reasoning token 也计入 max_tokens：小预算的 JSON 调用会被截断成空文本，',
            '    延迟还会涨到十几秒。这里默认关闭推理（可用 HELPDESK_DISABLE_THINKING=0 打开），',
            '    服务商不支持该参数时自动去掉重试，只提示一次，保证换服务商也能跑。',
            '    """',
            '    global _THINKING_DISABLED_SUPPORTED',
            '    kwargs: Dict[str, Any] = {',
            '        "model": model,',
            '        "max_tokens": max_tokens or helper_max_tokens(),',
            '        "temperature": temperature,',
            '        "messages": [{"role": "user", "content": prompt}],',
            '    }',
            '    extra = _thinking_extra_body()',
            '    if extra is not None:',
            '        try:',
            '            resp = await client.messages.create(extra_body=extra, **kwargs)',
            '            _THINKING_DISABLED_SUPPORTED = True',
            '            return resp',
            '        except Exception as ex:',
            '            text = str(ex).lower()',
            '            if "thinking" in text or "unexpected" in text or "invalid" in text or "400" in text:',
            '                _THINKING_DISABLED_SUPPORTED = False',
            '                logger.warning("服务商不支持 thinking=disabled，已自动关闭该优化: %s", ex)',
            '            else:',
            '                raise',
            '    return await client.messages.create(**kwargs)'
        )
    }
)

# ── 6 处辅助调用改成 create_helper_message ──────────────────────────────────
$helperCall = {
    param($clientExpr, $modelExpr, $temp)
    @(
        "            resp = await create_helper_message(",
        "                $clientExpr,",
        "                model=$modelExpr,",
        "                temperature=$temp,",
        "                prompt=prompt,",
        "            )"
    )
}

Apply-Changes 'core\intent_recognizer.py' @(
    @{
        Old = @(
            'from core.llm_utils import (',
            '    extract_text_content,',
            '    helper_max_tokens,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
        New = @(
            'from core.llm_utils import (',
            '    create_helper_message,',
            '    extract_text_content,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
    }
    @{
        Old = @(
            '            resp = await self.client.messages.create(',
            '                model=resolve_helper_model(self.model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.1,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )'
        )
        New = & $helperCall 'self.client' 'resolve_helper_model(self.model)' '0.1'
    }
)

Apply-Changes 'mcp\tool_manager.py' @(
    @{
        Old = @(
            'from core.llm_utils import (',
            '    extract_text_content,',
            '    helper_max_tokens,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
        New = @(
            'from core.llm_utils import (',
            '    create_helper_message,',
            '    extract_text_content,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.3,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )'
        )
        New = & $helperCall 'self._client' 'resolve_helper_model(self._model)' '0.3'
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )'
        )
        New = & $helperCall 'self._client' 'resolve_helper_model(self._model)' '0.0'
    }
)

Apply-Changes 'evaluation\evaluator.py' @(
    @{
        Old = @(
            'from core.llm_utils import (',
            '    extract_text_content,',
            '    helper_max_tokens,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
        New = @(
            'from core.llm_utils import (',
            '    create_helper_message,',
            '    extract_text_content,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )'
        )
        New = & $helperCall 'self._client' 'resolve_helper_model(self._model)' '0.0'
    }
)

Apply-Changes 'memory\conversation_memory.py' @(
    @{
        Old = @(
            'from core.llm_utils import (',
            '    extract_text_content,',
            '    helper_max_tokens,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
        New = @(
            'from core.llm_utils import (',
            '    create_helper_message,',
            '    extract_text_content,',
            '    parse_json_response,',
            '    resolve_helper_model,',
            ')'
        )
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            profile_data = parse_json_response(extract_text_content(resp.content))'
        )
        New = (@(& $helperCall 'self._client' 'resolve_helper_model(self._model)' '0.0') + @(
            '            profile_data = parse_json_response(extract_text_content(resp.content))'
        ))
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            summary = self._safe_text(extract_text_content(resp.content)).strip()'
        )
        New = (@(& $helperCall 'self._client' 'resolve_helper_model(self._model)' '0.0') + @(
            '            summary = self._safe_text(extract_text_content(resp.content)).strip()'
        ))
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            merged = self._safe_text(extract_text_content(resp.content)).strip()'
        )
        New = (@(& $helperCall 'self._client' 'resolve_helper_model(self._model)' '0.0') + @(
            '            merged = self._safe_text(extract_text_content(resp.content)).strip()'
        ))
    }
)

# ── .env：把开关写清楚 ─────────────────────────────────────────────────────
$envPath = Join-Path $Root '.env'
if (Test-Path -LiteralPath $envPath) {
    $envText = [System.IO.File]::ReadAllText($envPath, $enc)
    if ($envText -notmatch 'HELPDESK_DISABLE_THINKING') {
        $add = @(
            '',
            '# 辅助调用关闭推理（reasoning token 会挤爆预算并拖慢延迟）；置 0 可恢复推理',
            'HELPDESK_DISABLE_THINKING=1'
        ) -join "`r`n"
        [System.IO.File]::WriteAllText($envPath, $envText.TrimEnd() + "`r`n" + $add + "`r`n", $enc)
        Write-Host '  已更新 .env（新增 HELPDESK_DISABLE_THINKING）' -ForegroundColor Green
        $changed++
    } else {
        Write-Host '  跳过 .env' -ForegroundColor DarkGray
    }
}

Write-Host ''
Write-Host "完成：修改 $changed 处，跳过 $skipped 处补丁片段" -ForegroundColor Cyan
