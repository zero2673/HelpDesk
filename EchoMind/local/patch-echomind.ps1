# HelpDesk 补丁：修掉"推理模型吃光 max_tokens 导致 JSON 辅助调用失败"等 4 个问题
#
# 改动清单：
#   1. core/llm_utils.py         新增 resolve_helper_model / helper_max_tokens / parse_json_response
#   2. core/intent_recognizer.py  LLM 意图识别：改用辅助模型 + 1024 tokens + 带截断检测的 JSON 解析
#   3. mcp/tool_manager.py        查询改写、结果重排：同上
#   4. evaluation/evaluator.py    LLM-as-Judge：同上；并在结果里显式标注"裁判失败，分数为兜底值"
#   5. memory/conversation_memory.py  画像提炼 / 摘要 / 摘要合并：同上；空摘要不再写入；
#                                跨会话情景记忆检索的 where 改成 ChromaDB 要求的 $and 语法
#   6. monitor/performance_monitor.py 延迟阈值可用 HELPDESK_MONITOR_AGENT_AVG_MS 覆盖
#   7. .env                      追加 HELPDESK_FAST_MODEL / HELPDESK_HELPER_MAX_TOKENS /
#                                HELPDESK_MONITOR_AGENT_AVG_MS / HELPDESK_MONITOR_FALLBACK_PENALTY
#
# 特点：精确匹配、匹配不到就报错中止；已打过补丁的文件会自动跳过；改前自动备份到 local\backup-before-patch\
#
# 用法（项目根目录）：
#   powershell -ExecutionPolicy Bypass -File .\local\patch-helpdesk.ps1

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$Backup  = Join-Path $Root 'local\backup-before-patch'
$enc     = New-Object System.Text.UTF8Encoding($false)   # 项目里的 .py 是 UTF-8 无 BOM
$changed = 0
$skipped = 0

function Apply-Changes {
    param([string]$RelPath, [object[]]$Changes)

    $path = Join-Path $Root $RelPath
    if (-not (Test-Path -LiteralPath $path)) { throw "文件不存在：$path" }

    # 备份（只备份一次）
    $bkPath = Join-Path $Backup $RelPath
    if (-not (Test-Path -LiteralPath $bkPath)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $bkPath -Parent) | Out-Null
        Copy-Item -LiteralPath $path -Destination $bkPath -Force
    }

    $text = [System.IO.File]::ReadAllText($path, $enc)
    $nl   = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $dirty = $false

    foreach ($c in $Changes) {
        $old = ($c.Old -join $nl)
        $new = ($c.New -join $nl)
        $count = ([regex]::Matches($text, [regex]::Escape($old))).Count

        if ($count -eq 1) {
            $text  = $text.Replace($old, $new)
            $dirty = $true
        } elseif ($count -eq 0) {
            # 已经改过？用新内容的第一行判断
            if ($text.Contains($c.New[0])) {
                $script:skipped++
                continue
            }
            throw "$RelPath 匹配失败（期望 1 处，实际 0 处），未做任何修改。锚点：$($c.Old[0])"
        } else {
            throw "$RelPath 匹配到 $count 处（期望 1 处），锚点不够唯一，未做任何修改。锚点：$($c.Old[0])"
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

Write-Host "开始打补丁，备份目录：$Backup" -ForegroundColor Cyan

# ── 1. core/llm_utils.py：新增共享工具函数 ────────────────────────────────────
Apply-Changes 'core\llm_utils.py' @(
    @{
        Old = @(
            '"""LLM response helpers shared by Anthropic-compatible providers."""',
            'from typing import Any, Iterable, List'
        )
        New = @(
            '"""LLM response helpers shared by Anthropic-compatible providers."""',
            'import json',
            'import os',
            'import re',
            'from typing import Any, Iterable, List'
        )
    }
    @{
        Old = @(
            '        if isinstance(text, str) and (block_type in (None, "text")):',
            '            texts.append(text)',
            '',
            '    return "\n".join(t for t in texts if t)'
        )
        New = @(
            '        if isinstance(text, str) and (block_type in (None, "text")):',
            '            texts.append(text)',
            '',
            '    return "\n".join(t for t in texts if t)',
            '',
            '',
            'def resolve_helper_model(default_model: str) -> str:',
            '    """',
            '    辅助调用（意图识别 / 查询改写 / 重排 / LLM-as-Judge / 摘要 / 用户画像）专用的模型。',
            '',
            '    推理模型的 reasoning token 也计入 max_tokens，在这些小预算 JSON 调用上',
            '    很容易把预算烧光、只返回空文本。用 HELPDESK_FAST_MODEL 指定一个更快的模型',
            '    （例如 deepseek-flash）可以同时解决截断和延迟；不配置则回落到主模型，',
            '    保持原有行为。',
            '    """',
            '    return (os.getenv("HELPDESK_FAST_MODEL", "") or "").strip() or default_model',
            '',
            '',
            'def helper_max_tokens(default: int = 1024) -> int:',
            '    """辅助调用的 token 预算。默认 1024，给推理模型留出 reasoning 空间。"""',
            '    try:',
            '        return max(64, int(os.getenv("HELPDESK_HELPER_MAX_TOKENS", str(default))))',
            '    except (TypeError, ValueError):',
            '        return default',
            '',
            '',
            '_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")',
            '',
            '',
            'def extract_json_payload(raw: str, open_char: str = "{", close_char: str = "}") -> str:',
            '    """',
            '    取出模型回复里的 JSON 片段，并显式区分"没有 JSON"和"JSON 被截断"。',
            '',
            '    原实现直接写 raw[raw.find("{"):raw.rfind("}")+1]：一旦回复被 max_tokens',
            '    截断（没有闭合括号），rfind 返回 -1，切片变成空串，最终只抛出',
            '    "Expecting value: line 1 column 1 (char 0)"，很难定位。',
            '    """',
            '    text = _FENCE_RE.sub("", (raw or "").strip())',
            '    start = text.find(open_char)',
            '    if start < 0:',
            '        raise ValueError(',
            '            f"模型回复里没有 JSON（长度 {len(text)}，片段 {text[:80]!r}）"',
            '        )',
            '    end = text.rfind(close_char)',
            '    if end <= start:',
            '        raise ValueError(',
            '            f"模型回复的 JSON 不完整，疑似被 max_tokens 截断"',
            '            f"（长度 {len(text)}，片段 {text[start:start + 80]!r}）"',
            '        )',
            '    return text[start:end + 1]',
            '',
            '',
            'def parse_json_response(raw: str, open_char: str = "{", close_char: str = "}") -> Any:',
            '    """取出并解析 JSON；结构异常时抛出带原因的异常，由调用方决定兜底策略。"""',
            '    return json.loads(extract_json_payload(raw, open_char, close_char))'
        )
    }
)

# ── 2. core/intent_recognizer.py ─────────────────────────────────────────────
Apply-Changes 'core\intent_recognizer.py' @(
    @{
        Old = @('from core.llm_utils import extract_text_content')
        New = @('from core.llm_utils import (',
                '    extract_text_content,',
                '    helper_max_tokens,',
                '    parse_json_response,',
                '    resolve_helper_model,',
                ')')
    }
    @{
        Old = @(
            '            resp = await self.client.messages.create(',
            '                model=self.model,',
            '                max_tokens=256,',
            '                temperature=0.1,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            raw = extract_text_content(resp.content)',
            '            s, e = raw.find("{"), raw.rfind("}") + 1',
            '            data = json.loads(raw[s:e])'
        )
        New = @(
            '            resp = await self.client.messages.create(',
            '                model=resolve_helper_model(self.model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.1,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            data = parse_json_response(extract_text_content(resp.content))'
        )
    }
)

# ── 3. mcp/tool_manager.py：查询改写 + 结果重排 ──────────────────────────────
Apply-Changes 'mcp\tool_manager.py' @(
    @{
        Old = @('from core.llm_utils import extract_text_content')
        New = @('from core.llm_utils import (',
                '    extract_text_content,',
                '    helper_max_tokens,',
                '    parse_json_response,',
                '    resolve_helper_model,',
                ')')
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=self._model, max_tokens=256, temperature=0.3,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            raw = extract_text_content(resp.content)',
            '            s, e = raw.find("["), raw.rfind("]") + 1',
            '            queries = json.loads(raw[s:e])'
        )
        New = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.3,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            queries = parse_json_response(extract_text_content(resp.content), "[", "]")'
        )
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=self._model, max_tokens=256, temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            raw = extract_text_content(resp.content)',
            '            s, e = raw.find("["), raw.rfind("]") + 1',
            '            order: List[int] = json.loads(raw[s:e])'
        )
        New = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            order: List[int] = parse_json_response(',
            '                extract_text_content(resp.content), "[", "]"',
            '            )'
        )
    }
)

# ── 4. evaluation/evaluator.py：LLM-as-Judge ─────────────────────────────────
Apply-Changes 'evaluation\evaluator.py' @(
    @{
        Old = @('from core.llm_utils import extract_text_content')
        New = @('from core.llm_utils import (',
                '    extract_text_content,',
                '    helper_max_tokens,',
                '    parse_json_response,',
                '    resolve_helper_model,',
                ')')
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=self._model, max_tokens=256, temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            raw = extract_text_content(resp.content)',
            '            s, e = raw.find("{"), raw.rfind("}") + 1',
            '            data = json.loads(raw[s:e])'
        )
        New = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            data = parse_json_response(extract_text_content(resp.content))'
        )
    }
    @{
        Old = @(
            '                detail=f"Q: {question[:30]}... → 综合评分 {scores.overall:.3f}",'
        )
        New = @(
            '                detail=(f"Q: {question[:30]}... → 综合评分 {scores.overall:.3f}"',
            '                        + ("（LLM Judge 失败，以上为兜底值：{}）".format(scores.error)',
            '                           if scores.judge_failed else "")),'
        )
    }
)

# ── 5. memory/conversation_memory.py ─────────────────────────────────────────
Apply-Changes 'memory\conversation_memory.py' @(
    @{
        Old = @('from core.llm_utils import extract_text_content')
        New = @('from core.llm_utils import (',
                '    extract_text_content,',
                '    helper_max_tokens,',
                '    parse_json_response,',
                '    resolve_helper_model,',
                ')')
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=self._model, max_tokens=512, temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            raw = extract_text_content(resp.content)',
            '            s, e = raw.find("{"), raw.rfind("}") + 1',
            '            profile_data = json.loads(raw[s:e])'
        )
        New = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            profile_data = parse_json_response(extract_text_content(resp.content))'
        )
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=self._model, max_tokens=256, temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            summary = self._safe_text(extract_text_content(resp.content)).strip()',
            '        except Exception:',
            '            summary = f"对话包含 {len(to_compress)} 条消息（摘要生成失败）"'
        )
        New = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )',
            '            summary = self._safe_text(extract_text_content(resp.content)).strip()',
            '        except Exception as ex:',
            '            logger.warning(f"生成摘要失败: {ex}")',
            '            summary = ""',
            '        # 摘要为空时不要写入空串（推理模型被截断时会出现这种"成功但为空"的结果）',
            '        if not summary:',
            '            summary = f"对话包含 {len(to_compress)} 条消息（摘要生成失败）"'
        )
    }
    @{
        Old = @(
            '                where={"user_id": self._safe_text(user_id), "conv_id": self._safe_text(conv_id)},'
        )
        New = @(
            '                # ChromaDB 的多条件过滤必须用 $and 包裹，否则报',
            '                # "Expected where to have exactly one operator"',
            '                where={"$and": [',
            '                    {"user_id": self._safe_text(user_id)},',
            '                    {"conv_id": self._safe_text(conv_id)},',
            '                ]},'
        )
    }
    @{
        Old = @(
            '            resp = await self._client.messages.create(',
            '                model=self._model,',
            '                max_tokens=256,',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )'
        )
        New = @(
            '            resp = await self._client.messages.create(',
            '                model=resolve_helper_model(self._model),',
            '                max_tokens=helper_max_tokens(),',
            '                temperature=0.0,',
            '                messages=[{"role": "user", "content": prompt}],',
            '            )'
        )
    }
)

# ── 6. monitor/performance_monitor.py：延迟阈值可配 ──────────────────────────
Apply-Changes 'monitor\performance_monitor.py' @(
    @{
        Old = @('import asyncio', 'import logging', 'import statistics')
        New = @('import asyncio', 'import logging', 'import os', 'import statistics')
    }
    @{
        Old = @(
            '        self._webhook      = webhook_url',
            '        self._detector     = AnomalyDetector()'
        )
        New = @(
            '        self._webhook      = webhook_url',
            '        self._detector     = AnomalyDetector()',
            '',
            '        # 延迟阈值支持环境变量覆盖：默认 3000ms 是按本地小模型定的，',
            '        # 推理模型单轮 10s+ 很常见，会让所有 Agent 长期处于告警+降权状态。',
            '        self.THRESHOLDS = dict(type(self).THRESHOLDS)',
            '        override = (os.getenv("HELPDESK_MONITOR_AGENT_AVG_MS", "") or "").strip()',
            '        if override:',
            '            try:',
            '                _, _sev, _op = self.THRESHOLDS["agent_avg_ms"]',
            '                self.THRESHOLDS["agent_avg_ms"] = (float(override), _sev, _op)',
            '            except (TypeError, ValueError):',
            '                logger.warning("忽略非法配置 HELPDESK_MONITOR_AGENT_AVG_MS=%r", override)'
        )
    }
    @{
        Old = @(
            '    @staticmethod',
            '    def _routing_penalty(success_rate: float, avg_ms: float) -> float:',
            '        """把在线表现转成 0-0.9 的路由降权系数。"""',
            '        penalty = 0.0',
            '        if success_rate < 0.90:',
            '            penalty += min(0.5, (0.90 - success_rate) * 2)',
            '        if avg_ms > 3000:',
            '            penalty += min(0.4, (avg_ms - 3000) / 10000)',
            '        return min(penalty, 0.9)'
        )
        New = @(
            '    def _routing_penalty(self, success_rate: float, avg_ms: float) -> float:',
            '        """把在线表现转成 0-0.9 的路由降权系数。"""',
            '        penalty = 0.0',
            '        lat_threshold = self.THRESHOLDS["agent_avg_ms"][0]',
            '        if success_rate < 0.90:',
            '            penalty += min(0.5, (0.90 - success_rate) * 2)',
            '        if avg_ms > lat_threshold:',
            '            penalty += min(0.4, (avg_ms - lat_threshold) / 10000)',
            '        return min(penalty, 0.9)'
        )
    }
)

# ── 7. .env：追加配置 ───────────────────────────────────────────────────────
$envPath = Join-Path $Root '.env'
if (Test-Path -LiteralPath $envPath) {
    $envText = [System.IO.File]::ReadAllText($envPath, $enc)
    if ($envText -notmatch 'HELPDESK_FAST_MODEL') {
        $append = @(
            '',
            '# ── 辅助调用模型与预算（推理模型的 reasoning token 也占 max_tokens）────────',
            '# 意图识别 / 改写 / 重排 / LLM-as-Judge / 摘要 / 画像 走这个更快的模型',
            'HELPDESK_FAST_MODEL=deepseek-flash',
            'HELPDESK_HELPER_MAX_TOKENS=1024',
            '',
            '# ── 监控阈值（推理模型延迟高，按 3000ms 会把所有 Agent 判为异常）──────────',
            'HELPDESK_MONITOR_AGENT_AVG_MS=20000',
            'HELPDESK_MONITOR_FALLBACK_PENALTY=0.95'
        ) -join "`r`n"
        [System.IO.File]::WriteAllText($envPath, $envText.TrimEnd() + "`r`n" + $append + "`r`n", $enc)
        Write-Host '  已更新 .env' -ForegroundColor Green
        $changed++
    } else {
        Write-Host '  跳过 .env（已包含这些配置）' -ForegroundColor DarkGray
    }
}

Write-Host ''
Write-Host "完成：修改 $changed 处，跳过 $skipped 处补丁片段；原文件备份在 $Backup" -ForegroundColor Cyan
