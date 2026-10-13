from collections import deque

from agents.agent_orchestrator import (
    AccessAgent,
    AgentOrchestrator,
    AgentProfile,
    AgentType,
    EscalationAgent,
    GeneralAgent,
    NetworkAgent,
    Request,
    TechnicalAgent,
    build_shared_rag_tools,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def make_request(**kwargs):
    values = {
        "message": "我的笔记本开不了机了，设备编号 PC-1024，系统 Windows 11",
        "user_id": "emp10086",
        "conv_id": "c1",
        "intent": IntentCategory.DEVICE_FAILURE,
        "intent_group": "technical",
        "urgency": UrgencyLevel.MEDIUM,
        "intent_confidence": 0.92,
        "entities": {"device_id": ["PC-1024"], "os_version": ["Windows 11"], "error_code": []},
    }
    values.update(kwargs)
    return Request(**values)


def make_orchestrator():
    """构造带完整 Agent 池的编排器（不触发真实 LLM 调用）。"""
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [GeneralAgent(FakeClient(), "test-model")],
        AgentType.TECHNICAL: [TechnicalAgent(FakeClient(), "test-model")],
        AgentType.NETWORK: [NetworkAgent(FakeClient(), "test-model")],
        AgentType.ACCESS: [AccessAgent(FakeClient(), "test-model")],
        AgentType.ESCALATION: [EscalationAgent(FakeClient(), "test-model")],
    }
    orchestrator._recent_tool_traces = deque(maxlen=10)
    return orchestrator


def test_agent_profiles_are_distinct_and_tool_scopes_are_isolated():
    roles = {
        GeneralAgent.profile.role,
        TechnicalAgent.profile.role,
        NetworkAgent.profile.role,
        AccessAgent.profile.role,
        EscalationAgent.profile.role,
    }
    assert len(roles) == 5
    assert isinstance(GeneralAgent.profile, AgentProfile)
    assert TechnicalAgent.profile.temperature < GeneralAgent.profile.temperature

    assert "lookup_error_code" in TechnicalAgent.profile.tool_scope
    assert "lookup_asset" in TechnicalAgent.profile.tool_scope
    assert "check_access_request" in AccessAgent.profile.tool_scope
    assert "create_ticket" in EscalationAgent.profile.tool_scope

    # 权限 Agent 不该拿到错误码工具，网络 Agent 不该拿到权限/工单工具
    assert not set(AccessAgent.profile.tool_scope) & {"lookup_error_code"}
    assert not set(NetworkAgent.profile.tool_scope) & {"check_access_request", "create_ticket"}


def test_routing_table_maps_every_it_intent_to_its_owner_agent():
    orchestrator = make_orchestrator()
    expected = {
        IntentCategory.DEVICE_FAILURE: AgentType.TECHNICAL,
        IntentCategory.SOFTWARE_INSTALL: AgentType.TECHNICAL,
        IntentCategory.SYSTEM_ERROR: AgentType.TECHNICAL,
        IntentCategory.NETWORK_ISSUE: AgentType.NETWORK,
        IntentCategory.PERMISSION_REQUEST: AgentType.ACCESS,
        IntentCategory.ACCOUNT_ACCESS: AgentType.ACCESS,
        IntentCategory.IT_ESCALATION: AgentType.ESCALATION,
    }
    for intent, agent_type in expected.items():
        assert orchestrator._route(intent, UrgencyLevel.MEDIUM) is agent_type


def test_critical_urgency_and_escalation_intent_route_to_escalation_agent():
    orchestrator = make_orchestrator()
    assert orchestrator._route(IntentCategory.DEVICE_FAILURE, UrgencyLevel.CRITICAL) is AgentType.ESCALATION
    assert orchestrator._route(IntentCategory.IT_ESCALATION, UrgencyLevel.LOW) is AgentType.ESCALATION


def test_domain_scores_separate_network_and_access_issues():
    orchestrator = make_orchestrator()

    network = orchestrator._domain_scores(make_request(
        message="办公室网络连不上，VPN 也连不上，提示 691",
        intent=IntentCategory.NETWORK_ISSUE,
        entities={},
    ))
    assert network[AgentType.NETWORK] > network[AgentType.TECHNICAL]

    access = orchestrator._domain_scores(make_request(
        message="我需要申请财务共享盘的读写权限，工号 EMP10086",
        intent=IntentCategory.PERMISSION_REQUEST,
        entities={"employee_id": ["EMP10086"]},
    ))
    assert access[AgentType.ACCESS] > access[AgentType.TECHNICAL]


def test_composite_it_issue_keeps_primary_agent_and_adds_supporting_agent():
    orchestrator = make_orchestrator()
    decision = orchestrator._route_decision(make_request(
        message="VPN 连不上，而且财务共享盘也打不开，工号 EMP10086",
        intent=IntentCategory.NETWORK_ISSUE,
        entities={"employee_id": ["EMP10086"]},
    ))

    assert decision.primary_agent == AgentType.NETWORK
    assert AgentType.ACCESS in decision.supporting_agents
    assert "intent=network_issue" in decision.reason


def test_access_agent_role_packet_declares_security_boundary():
    packet = AccessAgent(FakeClient(), "test-model")._build_role_packet(
        make_request(intent=IntentCategory.PERMISSION_REQUEST, entities={"employee_id": ["EMP10086"]})
    )
    assert "verification_fields" in packet
    assert "risk_boundary" in packet
    assert "不代提交权限申请" in packet


def test_shared_rag_tool_is_available_to_all_agents():
    tools = build_shared_rag_tools(None)
    assert "search_knowledge_base" in tools
    assert tools["search_knowledge_base"].input_schema["required"] == ["query"]


def test_escalation_agent_tools_include_ticket_creation():
    tools = EscalationAgent(FakeClient(), "test-model").get_tools()
    assert "create_ticket" in tools
    assert "create_handoff_summary" in tools
    assert "lookup_error_code" not in tools
