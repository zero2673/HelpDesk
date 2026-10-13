"""LLM response helpers shared by Anthropic-compatible providers."""
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)


def resolve_helper_model(default_model: str) -> str:
    """
    辅助调用（意图识别 / 查询改写 / 重排 / LLM-as-Judge / 摘要 / 用户画像）专用的模型。

    推理模型的 reasoning token 也计入 max_tokens，在这些小预算 JSON 调用上
    很容易把预算烧光、只返回空文本。用 HELPDESK_FAST_MODEL 指定一个更快的模型
    （例如 deepseek-flash）可以同时解决截断和延迟；不配置则回落到主模型，
    保持原有行为。
    """
    return (os.getenv("HELPDESK_FAST_MODEL", "") or "").strip() or default_model


def helper_max_tokens(default: int = 1024) -> int:
    """辅助调用的 token 预算。默认 1024，给推理模型留出 reasoning 空间。"""
    try:
        return max(64, int(os.getenv("HELPDESK_HELPER_MAX_TOKENS", str(default))))
    except (TypeError, ValueError):
        return default


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def extract_json_payload(raw: str, open_char: str = "{", close_char: str = "}") -> str:
    """
    取出模型回复里的 JSON 片段，并显式区分"没有 JSON"和"JSON 被截断"。

    原实现直接写 raw[raw.find("{"):raw.rfind("}")+1]：一旦回复被 max_tokens
    截断（没有闭合括号），rfind 返回 -1，切片变成空串，最终只抛出
    "Expecting value: line 1 column 1 (char 0)"，很难定位。
    """
    text = _FENCE_RE.sub("", (raw or "").strip())
    start = text.find(open_char)
    if start < 0:
        raise ValueError(
            f"模型回复里没有 JSON（长度 {len(text)}，片段 {text[:80]!r}）"
        )
    end = text.rfind(close_char)
    if end <= start:
        raise ValueError(
            f"模型回复的 JSON 不完整，疑似被 max_tokens 截断"
            f"（长度 {len(text)}，片段 {text[start:start + 80]!r}）"
        )
    return text[start:end + 1]


def parse_json_response(raw: str, open_char: str = "{", close_char: str = "}") -> Any:
    """取出并解析 JSON；结构异常时抛出带原因的异常，由调用方决定兜底策略。"""
    return json.loads(extract_json_payload(raw, open_char, close_char))


# 服务商是否支持 thinking={"type": "disabled"}，None 表示还没探测过
_THINKING_DISABLED_SUPPORTED: Optional[bool] = None


def thinking_disabled_by_config() -> bool:
    """HELPDESK_DISABLE_THINKING=0/false/no/off 时保留推理，其余情况关闭推理。"""
    flag = (os.getenv("HELPDESK_DISABLE_THINKING", "1") or "").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _thinking_extra_body() -> Optional[Dict[str, Any]]:
    global _THINKING_DISABLED_SUPPORTED
    if _THINKING_DISABLED_SUPPORTED is False:
        return None
    if not thinking_disabled_by_config():
        return None
    return {"thinking": {"type": "disabled"}}


async def create_helper_message(
    client: Any,
    *,
    model: str,
    prompt: str,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
) -> Any:
    """
    辅助调用（意图 / 改写 / 重排 / Judge / 摘要 / 画像）统一入口。

    推理模型的 reasoning token 也计入 max_tokens：小预算的 JSON 调用会被截断成空文本，
    延迟还会涨到十几秒。这里默认关闭推理（可用 HELPDESK_DISABLE_THINKING=0 打开），
    服务商不支持该参数时自动去掉重试，只提示一次，保证换服务商也能跑。
    """
    global _THINKING_DISABLED_SUPPORTED
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or helper_max_tokens(),
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    extra = _thinking_extra_body()
    if extra is not None:
        try:
            resp = await client.messages.create(extra_body=extra, **kwargs)
            _THINKING_DISABLED_SUPPORTED = True
            return resp
        except Exception as ex:
            text = str(ex).lower()
            if "thinking" in text or "unexpected" in text or "invalid" in text or "400" in text:
                _THINKING_DISABLED_SUPPORTED = False
                logger.warning("服务商不支持 thinking=disabled，已自动关闭该优化: %s", ex)
            else:
                raise
    return await client.messages.create(**kwargs)
