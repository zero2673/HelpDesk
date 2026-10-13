"""Agent 工具定义与实现。

所有 Agent 工具集中在这里，编排器只负责：
  1. 根据 Agent 类型暴露工具白名单
  2. 执行 LLM 返回的 tool_use
  3. 将工具结果回传给 LLM

工具本身保持确定性、可测试，并明确区分：
  - 当前请求分析
  - 技术排障建议
  - 权限申请字段核验
  - 人工升级摘要
  - 共享知识库 RAG

订单查询、退款执行、账单修改等需要真实业务系统授权的动作不在这里伪造。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可见工具的定义和执行函数。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """创建带 JSON Schema 的 Agent 工具。"""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


def inspect_request_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """通用服务台工具：返回脱敏后的当前请求快照。"""
    return {
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.name if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": req.entities or {},
        "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """服务台工具：按问题类型计算下一轮只需询问的字段。"""
    intent = req.intent.value if req.intent else "other"
    fields: List[str] = []
    if intent in {"device_failure", "software_install", "system_error"}:
        fields = ["设备编号或主机名", "操作系统版本", "报错原文或截图描述"]
    elif intent == "network_issue":
        fields = ["影响范围（一个人还是一群人）", "有线还是无线/VPN", "错误提示或错误码"]
    elif intent in {"permission_request", "account_access"}:
        fields = ["工号", "目标系统或资源", "需要的权限级别", "审批人"]
    elif intent in {"it_escalation", "escalation"}:
        fields = ["问题现象", "已尝试的自助步骤", "影响范围"]
    elif intent == "other":
        fields = ["希望解决的具体问题", "设备或系统名称"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": req.entities or {},
    }

def lookup_error_code(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """终端/网络工具：解释常见错误码的排查方向，不声称读取了服务端日志。

    覆盖三类实际工单里最常见的错误码：
      1. HTTP 状态码（应用/接口层）
      2. Windows / Active Directory 域账号相关（0x 开头、LDAP）
      3. VPN 客户端错误（691/809 等）
    """
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        # ── HTTP ──
        "401": ("认证失败", ["确认登录态或 Token 是否过期", "重新登录后重试", "确认账号是否被锁定"]),
        "403": ("权限不足", ["确认当前账号的权限级别", "确认是否需要提交权限申请", "不要自行尝试绕过"]),
        "404": ("资源或路径不存在", ["确认访问地址是否正确", "确认资源是否已下线或迁移"]),
        "500": ("服务端处理异常", ["记录发生时间与完整报错", "间隔几分钟后重试", "持续出现则开工单并附截图"]),
        # ── Windows / Active Directory ──
        "0X80070005": ("访问被拒绝（权限不足）", ["确认当前账号是否有该目录/对象的权限", "确认是否以管理员身份运行", "必要时提交权限申请"]),
        "0X80070424": ("指定的服务不存在或未安装", ["确认系统组件是否完整", "确认是否被安全策略禁用", "需要管理员处理，转二线"]),
        "0X8007052E": ("用户名或密码不正确", ["确认大小写与输入法（全角/半角）", "确认账号是否近期改过密码", "连续失败会触发锁定，需等待或重置"]),
        "0X80070533": ("账号或密码已过期", ["按提示自助重置密码", "重置后同步等待 5-10 分钟再登录"]),
        "0X80070775": ("账号已被锁定", ["等待系统自动解锁（默认 30 分钟）", "或联系服务台人工解锁", "确认是否有其他设备用旧密码反复重试"]),
        "LDAP49": ("LDAP 凭证无效（密码错误或过期）", ["确认密码是否正确且未过期", "确认域账号状态是否正常"]),
        "LDAP32": ("LDAP 条目不存在", ["确认用户名或工号是否正确", "确认账号是否已创建或已离职停用"]),
        "LDAP50": ("LDAP 权限不足", ["确认是否有读取该目录对象的权限", "需要域管理员处理，转二线"]),
        # ── VPN ──
        "691": ("VPN 认证失败", ["确认域账号密码是否正确", "确认账号是否被锁定", "确认 VPN 权限是否已开通"]),
        "809": ("VPN 连接超时或被阻断", ["确认本地网络可访问外网", "切换网络（家庭宽带/手机热点）重试", "确认 VPN 地址与端口是否可达"]),
        "868": ("VPN 域名解析失败", ["切换 DNS 或重连网络", "确认 VPN 服务地址拼写正确", "持续失败转网络组"]),
        "720": ("VPN 拨号协议错误", ["确认客户端版本是否最新", "重置网络适配器后重连", "仍失败则转二线"]),
    }
    meaning, steps = mapping.get(
        code,
        ("暂未收录的错误码", ["补充完整错误码与截图", "说明发生场景（登录/安装/联网）", "需要时由服务台开单转二线"]),
    )
    return {
        "error_code": code,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
    }

def build_diagnostic_plan(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """终端/网络工具：生成低风险、可自助执行的排障顺序。"""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    steps = [
        "记录完整现象与报错原文（截图或错误码）",
        "确认影响范围：只有自己还是同一区域/同一系统的多人",
        "确认最近变更：是否刚更新过系统、装过软件、改过密码",
        "做最小验证：换账号、换网络、换设备各试一次",
    ]
    if reproduced:
        steps.append("整理可稳定复现的操作步骤，交给二线可直接复现")
    return {
        "environment": environment,
        "reproduced": reproduced,
        "diagnostic_steps": steps,
    }

def check_access_request(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """权限工具：检查权限申请必要字段是否齐全并给出审批路径。"""
    fields = {
        "employee_id": bool(req.entities.get("employee_id")),
        "target_system": bool(req.entities.get("app")) or bool(args.get("target_system")),
        "permission_level": bool(args.get("permission_level")),
        "approver": bool(args.get("approver")),
        "duration": bool(args.get("duration")),
    }
    level = str(args.get("permission_level", "")).lower()
    high_risk = any(kw in level for kw in ("admin", "管理员", "写", "write", "生产", "prod"))
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "high_risk": high_risk,
        "approval_path": "高权限/生产环境：直属主管 + 系统负责人 + IT 安全审批" if high_risk
                         else "常规权限：直属主管审批后由 IT 开通，通常 1 个工作日内生效",
        "can_grant": False,
        "reason": "服务台只做字段检查与路径说明，不代替申请人提交、不直接开通权限",
    }


def lookup_asset(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """资产工具：按设备编号或工号查询资产归属信息（示例实现，未连接真实资产系统）。"""
    device_id = str(args.get("device_id", "") or (req.entities.get("device_id") or [""])[0]).strip()
    employee_id = str(args.get("employee_id", "") or (req.entities.get("employee_id") or [""])[0]).strip()
    if not device_id and not employee_id:
        return {"success": False, "error": "需要提供设备编号或工号", "source": "asset-system(mock)"}
    return {
        "success": True,
        "query": {"device_id": device_id, "employee_id": employee_id},
        "asset": {
            "device_id": device_id or "待确认",
            "model": "巡检示例机型（未接真实资产系统）",
            "os": "Windows 11 专业版",
            "warranty": "保修期内",
            "owner": employee_id or "待确认",
        },
        "source": "asset-system(mock)",
        "note": "示例数据，接入真实资产系统后替换该实现即可，接口契约保持不变",
    }

def create_ticket(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """工单工具：生成结构化工单摘要（示例实现，未连接真实工单系统）。"""
    summary = str(args.get("summary", "") or req.message)[:200]
    category = str(args.get("category", "other"))[:40]
    urgency = req.urgency.name if req.urgency else "MEDIUM"
    priority = "P1" if urgency == "CRITICAL" else ("P2" if urgency == "HIGH" else "P3")
    sla = {"P1": "30 分钟内响应，4 小时内给出进展", "P2": "2 小时内响应，1 个工作日内处理", "P3": "1 个工作日内响应，3 个工作日内处理"}[priority]
    return {
        "success": True,
        "ticket": {
            "ticket_id": f"IT-{abs(hash((req.request_id, summary))) % 100000:05d}",
            "summary": summary,
            "category": category,
            "priority": priority,
            "sla": sla,
            "requester": req.user_id,
            "entities": req.entities or {},
        },
        "source": "ticket-system(mock)",
        "note": "示例实现；接入真实工单系统（Jira / ServiceNow / 自研）时替换该函数，入参与返回结构保持不变",
    }

def create_handoff_summary(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """升级工具：生成可交给 IT 二线的结构化交接摘要。"""
    return {
        "request_id": req.request_id,
        "reason": str(args.get("reason", "需要 IT 二线继续处理"))[:120],
        "intent": req.intent.value if req.intent else "unknown",
        "urgency": req.urgency.name if req.urgency else "UNKNOWN",
        "entities": req.entities or {},
        "tried_steps": args.get("tried_steps", []),
        "sensitive_data_required": False,
    }

def build_shared_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """构建所有 Agent 可共享的 RAG 工具。"""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = int(args.get("top_k", 5) or 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", "知识库检索失败"),
                "results": [],
                "reranked": False,
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "检索知识库并返回最相关的文档片段；可用于通用、技术、账单和升级场景。",
            {
                "query": {"type": "string", "description": "用户问题或检索关键词"},
                "top_k": {"type": "integer", "description": "返回结果条数"},
            },
            search_knowledge_base,
            required=["query"],
        )
    }


def general_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "查看当前请求的意图、紧急度、实体和上下文可用性；不查询外部业务系统。",
            {"focus": {"type": "string", "description": "希望关注的业务方向"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "根据当前意图建议下一轮只需向用户补充的字段。",
            {},
            suggest_required_fields,
        ),
    }


def technical_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "解释常见错误码（HTTP、Windows/AD 域账号、VPN 客户端）的含义和低风险排查方向；不读取服务端日志。",
            {"error_code": {"type": "string", "description": "例如 401、0x80070005、LDAP 49、VPN 691"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "按影响范围与是否可复现生成自助排查顺序，不执行修改配置等操作。",
            {
                "environment": {"type": "string", "description": "员工的设备/系统场景，例如 Windows 11 笔记本、Outlook 客户端"},
                "reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
        "lookup_asset": make_tool(
            "lookup_asset",
            "按设备编号或工号查询资产归属、机型、操作系统与保修状态；示例实现，未连接真实资产系统。",
            {
                "device_id": {"type": "string", "description": "设备编号，例如 PC-1024"},
                "employee_id": {"type": "string", "description": "工号，例如 EMP10086"},
            },
            lookup_asset,
        ),
    }


def network_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "解释 VPN 与网络相关错误码（691/809/868/720）的含义和自助排查方向。",
            {"error_code": {"type": "string", "description": "例如 691、809、868"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "按影响范围生成联网问题的自助排查顺序，不涉及交换机或防火墙配置。",
            {
                "environment": {"type": "string", "description": "例如 办公区有线、家庭宽带 VPN、手机热点"},
                "reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
    }


def access_tools() -> Dict[str, AgentToolSpec]:
    return {
        "check_access_request": make_tool(
            "check_access_request",
            "检查权限申请字段是否齐全，判断是否属于高权限，并给出审批路径；不代替提交、不开通权限。",
            {
                "target_system": {"type": "string", "description": "目标系统或资源，例如财务共享盘、生产数据库"},
                "permission_level": {"type": "string", "description": "权限级别，例如只读、读写、管理员"},
                "approver": {"type": "string", "description": "审批人，例如直属主管"},
                "duration": {"type": "string", "description": "权限有效期，例如 3 个月、长期"},
            },
            check_access_request,
        ),
        "lookup_asset": make_tool(
            "lookup_asset",
            "按工号或设备编号查询资产与归属，用于核对申请人的设备信息；示例实现。",
            {
                "device_id": {"type": "string", "description": "设备编号"},
                "employee_id": {"type": "string", "description": "工号"},
            },
            lookup_asset,
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "create_ticket": make_tool(
            "create_ticket",
            "生成 IT 工单摘要与优先级/SLA（示例实现，未连接真实工单系统）。",
            {
                "summary": {"type": "string", "description": "工单摘要：现象、影响范围、已尝试步骤"},
                "category": {"type": "string", "description": "分类，例如 device/network/access/software"},
            },
            create_ticket,
            required=["summary"],
        ),
        "create_handoff_summary": make_tool(
            "create_handoff_summary",
            "生成交给 IT 二线的结构化交接摘要。",
            {"reason": {"type": "string", "description": "需要升级的原因"}},
            create_handoff_summary,
        ),
    }