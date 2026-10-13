from mcp.knowledge_base import KnowledgeBase


def make_kb():
    return KnowledgeBase.__new__(KnowledgeBase)


def test_chunk_text_keeps_short_document_as_single_chunk():
    chunks = make_kb()._chunk_text("这是一段很短的 IT 服务台说明。")
    assert chunks == ["这是一段很短的 IT 服务台说明。"]


def test_chunk_text_splits_long_document_into_multiple_chunks():
    long_text = "".join(f"第{i}条权限申请规范说明。" for i in range(200))
    chunks = make_kb()._chunk_text(long_text)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    # 分块可能丢弃句间分隔符，这里只要求内容基本完整保留
    assert len("".join(chunks)) >= len(long_text) * 0.9
