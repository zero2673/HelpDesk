import pytest

from core.intent_recognizer import (
    IntentCategory,
    IntentRecognizer,
    UrgencyLevel,
    _INTENT_GROUPS,
    _SPECIFIC_INTENTS,
    _TEMPLATES,
)

IT_INTENTS = [
    IntentCategory.DEVICE_FAILURE,
    IntentCategory.SOFTWARE_INSTALL,
    IntentCategory.NETWORK_ISSUE,
    IntentCategory.PERMISSION_REQUEST,
    IntentCategory.ACCOUNT_ACCESS,
    IntentCategory.SYSTEM_ERROR,
    IntentCategory.IT_ESCALATION,
]


def make_recognizer(threshold=0.5):
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer._embedding_enabled = True
    recognizer.threshold = threshold
    return recognizer


def vote(recognizer, llm, emb, pat):
    return recognizer._vote(
        {"intent": llm[0], "confidence": llm[1]},
        {"intent": emb[0], "confidence": emb[1]},
        {"intent": pat[0], "confidence": pat[1]},
    )


def test_every_it_intent_has_few_shot_templates():
    for intent in IT_INTENTS:
        assert _TEMPLATES.get(intent), f"{intent} 缺少 few-shot 模板"
        assert intent in _SPECIFIC_INTENTS


def test_it_intents_are_grouped_into_domain_buckets():
    assert _INTENT_GROUPS[IntentCategory.DEVICE_FAILURE] is IntentCategory.TECHNICAL
    assert _INTENT_GROUPS[IntentCategory.SYSTEM_ERROR] is IntentCategory.TECHNICAL
    assert _INTENT_GROUPS[IntentCategory.NETWORK_ISSUE] is IntentCategory.TECHNICAL
    assert _INTENT_GROUPS[IntentCategory.PERMISSION_REQUEST] is IntentCategory.ACCOUNT
    assert _INTENT_GROUPS[IntentCategory.ACCOUNT_ACCESS] is IntentCategory.ACCOUNT
    assert _INTENT_GROUPS[IntentCategory.IT_ESCALATION] is IntentCategory.ESCALATION


def test_pattern_cannot_refine_a_different_intent_group():
    intent, confidence, sources = vote(
        make_recognizer(),
        (IntentCategory.ACCOUNT, 0.9),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.NETWORK_ISSUE, 0.5),
    )

    assert intent is IntentCategory.ACCOUNT
    assert confidence == pytest.approx(0.63)
    assert "refined_by_pattern" not in sources


def test_pattern_refines_within_the_winning_group():
    intent, confidence, sources = vote(
        make_recognizer(),
        (IntentCategory.TECHNICAL, 0.8),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.SYSTEM_ERROR, 0.5),
    )

    assert intent is IntentCategory.SYSTEM_ERROR
    assert confidence == pytest.approx(0.61)
    assert sources["refined_by_pattern"] == pytest.approx(0.5)


def test_threshold_falls_back_to_other_when_scores_are_low():
    intent, confidence, _ = vote(
        make_recognizer(threshold=0.8),
        (IntentCategory.TECHNICAL, 0.4),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.QUERY, 0.1),
    )
    assert intent is IntentCategory.OTHER
    assert confidence < 0.8


def test_entities_cover_device_employee_os_app_and_error_code():
    recognizer = make_recognizer()
    entities = recognizer._extract_entities(
        "我的设备编号 PC-1024 连不上 VPN，工号 EMP10086，系统 Windows 11，Outlook 也打不开，提示错误 691"
    )

    assert "PC-1024" in entities["device_id"]
    assert "EMP10086" in entities["employee_id"]
    assert any("Windows 11" in value for value in entities["os_version"])
    assert "691" in entities["error_code"]
    assert any(value.lower() == "vpn" for value in entities["app"])


def test_urgency_marks_multi_user_outage_as_critical():
    recognizer = make_recognizer()
    assert recognizer._urgency("整个部门的网络都断了，非常紧急", IntentCategory.NETWORK_ISSUE) is UrgencyLevel.CRITICAL
    assert recognizer._urgency("我今天想装一个 Visio", IntentCategory.SOFTWARE_INSTALL) is UrgencyLevel.HIGH
    assert recognizer._urgency("有空的时候帮我看看打印机", IntentCategory.DEVICE_FAILURE) is UrgencyLevel.LOW
