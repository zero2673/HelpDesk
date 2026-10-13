"""
亮点：端到端意图识别

三路融合策略：
  1. LLM 语义理解（权重 70%）—— 主力，理解复杂语义和上下文
  2. Embedding 向量相似度（权重 20%）—— 快速匹配常见表达
  3. 关键词模式匹配（权重 10%）—— 零延迟兜底

三路结果通过加权投票合并，置信度低于阈值时降级为 OTHER。
LLM 和 Embedding 并行调用，不串行等待。
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import (
    create_helper_message,
    extract_text_content,
    parse_json_response,
    resolve_helper_model,
)

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    # 通用意图（同时作为领域分组使用）
    QUERY      = "query"       # 查询信息
    COMPLAINT  = "complaint"   # 不满/催办
    REQUEST    = "request"     # 请求操作
    GREETING   = "greeting"    # 问候
    ESCALATION = "escalation"  # 要求升级/找主管
    TECHNICAL  = "technical"   # 终端与系统类
    ACCOUNT    = "account"     # 账号与权限类
    FEEDBACK   = "feedback"    # 正面反馈
    # ── IT 服务台细粒度意图 ──────────────────────────────────────────────
    DEVICE_FAILURE     = "device_failure"      # 设备/终端故障（开不了机、蓝屏、卡顿、外设）
    SOFTWARE_INSTALL   = "software_install"    # 软件安装/更新/授权/卸载重装
    NETWORK_ISSUE      = "network_issue"       # 网络与连通性（断网、Wi-Fi、VPN、代理、内网）
    PERMISSION_REQUEST = "permission_request"  # 系统/共享盘/数据库/生产环境权限申请
    ACCOUNT_ACCESS     = "account_access"      # 账号与密码（重置密码、账号锁定、MFA）
    SYSTEM_ERROR       = "system_error"        # 系统/应用报错（含错误码）
    IT_ESCALATION      = "it_escalation"       # 需要 IT 二线 / 开正式工单
    OTHER      = "other"

class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    intent_group: str
    entities:   Dict[str, List[str]]   # 从消息中提取的实体
    reasoning:  str
    latency_ms: float
    source_scores: Dict[str, float] = field(default_factory=dict)


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.QUERY:      ["IT 服务台能处理哪些问题？", "工单提交后多久响应？", "怎么申请 VPN 权限？"],
    IntentCategory.COMPLAINT:  ["工单提交三天了没人处理！", "电脑坏了等了一周", "这个流程太慢了"],
    IntentCategory.REQUEST:    ["帮我重置一下密码", "请协助申请共享盘权限", "麻烦安排工程师上门"],
    IntentCategory.GREETING:   ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.ESCALATION: ["我要找 IT 主管", "工单要升级", "对处理结果不满意"],
    IntentCategory.TECHNICAL:  ["电脑有问题", "软件用不了", "系统报错"],
    IntentCategory.ACCOUNT:    ["账号有问题", "密码不对", "权限不够"],
    IntentCategory.FEEDBACK:   ["解决了，谢谢！", "工程师很专业", "响应很快"],
    IntentCategory.DEVICE_FAILURE:     ["笔记本开不了机了", "电脑老是蓝屏", "显示器黑屏没信号", "键盘有几个键不灵"],
    IntentCategory.SOFTWARE_INSTALL:   ["帮我装一下 Visio", "Outlook 想升级到最新版", "这个软件没有授权怎么激活"],
    IntentCategory.NETWORK_ISSUE:      ["办公室网络连不上", "VPN 一直连不上", "内网 GitLab 打不开", "Wi-Fi 老是掉线"],
    IntentCategory.PERMISSION_REQUEST: ["申请生产数据库只读权限", "需要访问财务共享盘的权限", "申请一个测试环境账号"],
    IntentCategory.ACCOUNT_ACCESS:     ["我忘记域账号密码了", "账号被锁定了", "MFA 令牌收不到", "登录不上邮箱"],
    IntentCategory.SYSTEM_ERROR:       ["OA 系统报 500 错误", "打开文件提示 0x80070005", "客户端一直闪退"],
    IntentCategory.IT_ESCALATION:      ["帮我开个工单", "这个问题要转二线", "需要运维介入"],
}

_SPECIFIC_INTENTS = {
    IntentCategory.DEVICE_FAILURE,
    IntentCategory.SOFTWARE_INSTALL,
    IntentCategory.NETWORK_ISSUE,
    IntentCategory.PERMISSION_REQUEST,
    IntentCategory.ACCOUNT_ACCESS,
    IntentCategory.SYSTEM_ERROR,
    IntentCategory.IT_ESCALATION,
}

_GENERIC_INTENTS = {
    IntentCategory.QUERY,
    IntentCategory.COMPLAINT,
    IntentCategory.REQUEST,
    IntentCategory.GREETING,
    IntentCategory.ESCALATION,
    IntentCategory.TECHNICAL,
    IntentCategory.ACCOUNT,
    IntentCategory.FEEDBACK,
}

_INTENT_GROUPS: Dict[IntentCategory, IntentCategory] = {
    IntentCategory.DEVICE_FAILURE:     IntentCategory.TECHNICAL,
    IntentCategory.SOFTWARE_INSTALL:   IntentCategory.TECHNICAL,
    IntentCategory.SYSTEM_ERROR:       IntentCategory.TECHNICAL,
    IntentCategory.NETWORK_ISSUE:      IntentCategory.TECHNICAL,
    IntentCategory.PERMISSION_REQUEST: IntentCategory.ACCOUNT,
    IntentCategory.ACCOUNT_ACCESS:     IntentCategory.ACCOUNT,
    IntentCategory.IT_ESCALATION:      IntentCategory.ESCALATION,
}

# 紧急关键词：IT 场景里"影响多人 / 影响生产"才是真正的高优先级
_URGENCY_KEYWORDS = {
    UrgencyLevel.CRITICAL: ["紧急", "生产环境", "全部中断", "整个部门", "所有人都", "业务中断", "urgent", "asap", "p0"],
    UrgencyLevel.HIGH:     ["今天", "马上", "尽快", "演示前", "客户等着", "now", "hurry"],
    UrgencyLevel.MEDIUM:   ["这周", "本周内", "soon"],
}

def _cosine(a: List[float], b: List[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentRecognizer:
    """
    端到端意图识别器。

    初始化时不加载任何本地模型，所有 AI 能力通过 Anthropic API 调用。
    模板 Embedding 在首次请求时懒加载并缓存，后续复用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        # 本地字符 n-gram 向量始终可用；如果未来客户端暴露 embeddings 资源，
        # _embed_text 会优先尝试远端向量，否则自动回退本地向量。
        self._embedding_enabled = True

        self._tpl_embeddings: Dict[IntentCategory, List[List[float]]] = {}
        self._cache: Dict[str, IntentResult] = {}
        self.cache_hits   = 0
        self.cache_misses = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()

        # LLM 和 Embedding 并行（Embedding 不可用时跳过）
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message)) if self._embedding_enabled else None
        pat      = self._pattern_recognize(message)

        if emb_task:
            llm, emb = await asyncio.gather(llm_task, emb_task)
        else:
            llm = await llm_task
            emb = {"intent": IntentCategory.OTHER, "confidence": 0.0}

        intent, confidence, source_scores = self._vote(llm, emb, pat)
        entities = self._extract_entities(message)
        urgency  = self._urgency(message, intent)

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            urgency=urgency,
            intent_group=self._intent_group(intent),
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=source_scores,
        )

        # LRU 缓存
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
        self._cache[key] = result
        return result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """在线学习：将纠正样本加入模板，清除对应 Embedding 缓存。"""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._tpl_embeddings.pop(correct, None)  # 下次重新计算
            self._cache.clear()  # 模板更新后旧缓存可能对应过时结果
            logger.info(f"学习新样本 → {correct.value}: {message[:40]}")

    # ── 三路识别策略 ──────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """策略 1：LLM 语义理解（Few-shot + 上下文）。"""
        message = self._clean_text(message)
        # 构建 Few-shot 示例
        examples = "\n".join(
            f'  消息: "{t}" → 意图: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # 每类取 1 条，控制 prompt 长度
        )
        # 最近 3 轮对话上下文
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""你是企业 IT 服务台的问题分类专家。根据示例判断员工诉求的意图，返回 JSON。
如果用户问题能匹配细粒度意图，请优先返回细粒度意图，而不是宽泛大类。
例如设备打不开优先返回 device_failure，网络与 VPN 问题优先返回 network_issue，密码或账号被锁优先返回 account_access，权限申请优先返回 permission_request。
另外：用户明确要求"开单/转二线/找运维介入"时必须返回 it_escalation；要求"找主管/投诉/升级服务台"时返回 escalation；只是询问"怎么申请权限"这类流程问题，按问题本身的领域返回（例如权限申请返回 permission_request）。

        {ctx}
        用户消息: "{message}"

返回格式（仅 JSON，不要其他文字）:
{{"intent": "<意图值>", "confidence": <0-1>, "reasoning": "<一句话说明>"}}

可选意图: {", ".join(c.value for c in IntentCategory)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await create_helper_message(
                self.client,
                model=resolve_helper_model(self.model),
                temperature=0.1,
                prompt=prompt,
            )
            data = parse_json_response(extract_text_content(resp.content))
            try:
                data["intent"] = IntentCategory(data["intent"])
            except ValueError:
                data["intent"] = IntentCategory.OTHER
            return data
        except Exception as ex:
            logger.warning(f"LLM 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            await self._load_template_embeddings()
            msg_vec = await self._embed_text(message)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in self._tpl_embeddings.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": best_score}
        except Exception as ex:
            logger.warning(f"Embedding 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> Dict[str, Any]:
        """策略 3：关键词模式匹配（同步，零延迟兜底）。"""
        msg = message.lower()
        # IT 服务台关键词：优先匹配细粒度意图
        specific_patterns = {
            IntentCategory.IT_ESCALATION:      ["转it", "转工单", "开工单", "提工单", "二线", "运维介入", "升级处理", "找运维"],
            IntentCategory.DEVICE_FAILURE:     ["开不了机", "蓝屏", "死机", "黑屏", "没信号", "卡顿", "很卡", "风扇", "电池", "键盘", "鼠标", "显示器", "打印机", "外接屏", "设备编号", "硬件"],
            IntentCategory.SOFTWARE_INSTALL:   ["安装", "装一下", "卸载", "重装", "更新软件", "升级到", "正版", "授权", "激活", "office", "visio", "outlook", "teams", "驱动"],
            IntentCategory.NETWORK_ISSUE:      ["网络", "断网", "wifi", "无线", "有线", "vpn", "代理", "内网", "外网", "dns", "ping", "掉线", "连不上网", "打不开内网", "延迟高"],
            IntentCategory.PERMISSION_REQUEST: ["权限", "申请权限", "开通权限", "访问权限", "共享盘", "文件夹权限", "数据库权限", "生产环境", "测试环境", "审批", "白名单"],
            IntentCategory.ACCOUNT_ACCESS:     ["重置密码", "忘记密码", "密码错误", "账号锁定", "账号被锁", "域账号", "ad 账号", "mfa", "otp", "令牌", "登录不上", "邮箱登录"],
            IntentCategory.SYSTEM_ERROR:       ["报错", "错误码", "error", "崩溃", "闪退", "0x", "异常", "500"],
        }
        generic_patterns = {
            IntentCategory.ESCALATION: ["投诉", "主管", "经理", "supervisor"],
            IntentCategory.COMPLAINT:  ["太慢", "没人管", "等很久", "糟糕", "催一下"],
            IntentCategory.QUERY:      ["?", "？", "怎么", "如何", "什么", "能不能"],
            IntentCategory.REQUEST:    ["帮我", "需要", "麻烦", "please", "help"],
            IntentCategory.GREETING:   ["你好", "嗨", "hello", "hi"],
            IntentCategory.TECHNICAL:  ["电脑", "系统", "软件", "设备", "工位"],
            IntentCategory.ACCOUNT:    ["账号", "密码", "权限", "登录"],
        }
        best_cat, best_score = self._best_pattern_match(msg, specific_patterns)
        if best_cat != IntentCategory.OTHER:
            return {"intent": best_cat, "confidence": best_score}

        best_cat, best_score = self._best_pattern_match(msg, generic_patterns)
        return {"intent": best_cat, "confidence": best_score}

    # ── 投票合并 ──────────────────────────────────────────────────────────────

    def _vote(self, llm: Dict, emb: Dict, pat: Dict) -> tuple[IntentCategory, float, Dict[str, float]]:
        """加权投票。返回最终意图、融合置信度和各路来源得分。"""
        source_scores = {
            "llm": float(llm.get("confidence", 0.0) or 0.0),
            "embedding": float(emb.get("confidence", 0.0) or 0.0),
            "pattern": float(pat.get("confidence", 0.0) or 0.0),
        }
        if llm.get("failed"):
            if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
                return emb["intent"], source_scores["embedding"], source_scores
            if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
                return pat["intent"], source_scores["pattern"], source_scores
            return IntentCategory.OTHER, 0.0, source_scores

        if self._embedding_enabled:
            weights = [(llm, 0.7), (emb, 0.2), (pat, 0.1)]
        else:
            weights = [(llm, 0.85), (pat, 0.15)]
        scores: Dict[IntentCategory, float] = {}
        group_scores: Dict[IntentCategory, float] = {}
        for result, w in weights:
            cat  = result.get("intent", IntentCategory.OTHER)
            conf = result.get("confidence", 0.0)
            weighted_score = w * conf
            scores[cat] = scores.get(cat, 0.0) + weighted_score
            group = _INTENT_GROUPS.get(cat, cat)
            group_scores[group] = group_scores.get(group, 0.0) + weighted_score

        best_group = max(group_scores, key=group_scores.get)
        best_score = group_scores[best_group]
        best = max(
            (cat for cat in scores if _INTENT_GROUPS.get(cat, cat) == best_group),
            key=scores.get,
        )
        pat_intent = pat.get("intent", IntentCategory.OTHER)
        pat_conf = float(pat.get("confidence", 0.0) or 0.0)
        if (best in _GENERIC_INTENTS and pat_intent in _SPECIFIC_INTENTS
                and _INTENT_GROUPS.get(pat_intent, pat_intent) == best_group
                and pat_conf >= 0.5 and best_score < 0.8):
            source_scores["refined_by_pattern"] = pat_conf
            return pat_intent, max(best_score, pat_conf), source_scores
        if best_score < self.threshold:
            return IntentCategory.OTHER, best_score, source_scores
        return best, best_score, source_scores

    # ── 实体提取 ──────────────────────────────────────────────────────────────

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """用规则提取 IT 服务台高价值实体，避免每次识别都额外调用 LLM。"""
        message = self._clean_text(message)
        return {
            # 设备/资产编号：设备编号 ABC-1234、资产号、SN，或 PC-001 / LT-2231 这类资产命名
            "device_id": self._unique(
                re.findall(r"(?:设备编号|资产编号|资产号|序列号|主机名|sn|device[_ ]?id|asset[_ ]?id)\s*[:：#]?\s*([A-Za-z]{1,6}[- ]?\d{3,12})", message, re.I)
                + re.findall(r"\b((?:PC|NB|LT|DT|MAC|IT|WS|SRV)[- ]?\d{3,8})\b", message, re.I)
            ),
            # 工号：工号 10086 / EMP10086 / E1001
            "employee_id": self._unique(
                re.findall(r"(?:工号|员工号|工卡号|employee[_ ]?id|emp[_ ]?id)\s*[:：#]?\s*([A-Za-z]{0,3}\d{3,10})", message, re.I)
            ),
            # 操作系统版本
            "os_version": self._unique(
                re.findall(r"(Windows\s*(?:7|8|10|11|Server\s*\d{4})|Win\s*\d{1,2}|macOS\s*[\w.]*|mac\s*OS\s*[\w.]*|Ubuntu\s*[\d.]+|CentOS\s*[\d.]+|iOS\s*[\d.]+|Android\s*[\d.]+)", message, re.I)
            ),
            # 涉及的具体应用/系统
            "app": self._unique(
                re.findall(r"\b(Outlook|Teams|Word|Excel|PowerPoint|Visio|Chrome|Edge|Firefox|VPN|GitLab|Jenkins|Jira|Confluence|SAP|OA|钉钉|飞书|企业微信|微信)\b", message, re.I)
            ),
            # 错误码：0x80070005 / LDAP 49 / VPN 691 / HTTP 500 都能收
            "error_code": self._unique(
                re.findall(r"\b(0x[0-9a-fA-F]{4,8})\b", message)
                + re.findall(r"(?:error(?:_code)?|错误码?|状态码|http|ldap|ad|vpn)\s*[:：#]?\s*(\d{2,4})\b", message, re.I)
                + re.findall(r"\b([45]\d{2})\b", message)
            ),
            "date": self._unique(re.findall(r"(今天|昨天|刚才|早上|上午|下午|本周|这周|上周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", message)),
        }
    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """懒加载所有模板的 Embedding（只在首次调用时执行）。"""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        """
        生成文本向量。

        如果未来接入的官方/兼容客户端提供 embeddings.create，会优先使用远端向量；
        当前 Anthropic SDK 没有该资源时，退化为字符 n-gram 哈希向量。这样不会因为
        Embedding 服务缺失导致三路融合中断。
        """
        embeddings = getattr(self.client, "embeddings", None)
        if embeddings is not None:
            try:
                resp = await embeddings.create(model="voyage-3-lite", input=[text])
                return list(resp.data[0].embedding)
            except Exception as ex:
                logger.warning(f"远端 Embedding 失败，使用本地向量兜底: {ex}")

        return self._local_embedding(text)

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> List[float]:
        """稳定的字符 n-gram 哈希向量，用于无远端 Embedding 时的语义近似匹配。"""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        for level, kws in _URGENCY_KEYWORDS.items():
            if any(kw in msg for kw in kws):
                return level
        if intent in (IntentCategory.ESCALATION, IntentCategory.IT_ESCALATION):
            return UrgencyLevel.HIGH
        if intent == IntentCategory.COMPLAINT:
            return UrgencyLevel.MEDIUM
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload = {"message": self._clean_text(message)[:200]}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", ""))[:160],
                }
                for item in history[-3:]
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _best_pattern_match(
        message: str,
        patterns: Dict[IntentCategory, List[str]],
    ) -> tuple[IntentCategory, float]:
        best_cat, best_score = IntentCategory.OTHER, 0.0
        for cat, kws in patterns.items():
            hits = sum(1 for kw in kws if kw in message)
            if not hits:
                continue
            # 单个明确业务关键词就给可用置信度；多个关键词命中时提高置信度。
            score = min(1.0, 0.5 + 0.25 * (hits - 1))
            if score > best_score:
                best_score, best_cat = score, cat
        return best_cat, best_score

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, intent).value

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
