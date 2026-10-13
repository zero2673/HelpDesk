"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用服务端时不传 embedding_function，让服务端处理
        # 本地模式时也不传，使用 ChromaDB 默认的（会触发模型下载）
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "HelpDesk RAG 知识库"},
        )

        # 如果知识库为空，导入默认文档
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({"title": title, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # ID 由文档内容稳定生成；upsert 使重复导入幂等，同时允许同 ID 内容更新。
            # ChromaDB 会自动生成或更新 Embedding。
            self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库写入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """首次启动且知识库为空时，导入内置的 IT 服务台文档。"""
        default_docs = [
            {
                "title": "IT 服务台总览与工单流程",
                "content": (
                    "IT 服务台是员工提交 IT 问题的统一入口，受理范围包括：终端设备故障、软件安装与授权、"
                    "网络与 VPN、账号与密码、系统权限申请、办公系统使用问题。"
                    "一线上门或远程判断后：能自助解决的当场给步骤；需要后台操作的按类型开单；影响多人或生产环境的直接升级为高优先级。"
                    "工单优先级：P1 影响多人或业务中断，30 分钟内响应、4 小时内给出进展；P2 影响个人主要工作，2 小时内响应、1 个工作日内处理；"
                    "P3 一般咨询与体验问题，1 个工作日内响应、3 个工作日内处理。"
                    "提交工单时请尽量提供：设备编号或主机名、操作系统版本、报错原文或截图、影响范围、已经尝试过的步骤。"
                ),
            },
            {
                "title": "VPN 安装与连接故障排查",
                "content": (
                    "VPN 客户端下载渠道：内网软件中心（外网可访问的镜像地址请以 IT 公告为准），不要在第三方站点下载客户端。"
                    "安装后使用域账号登录，首次登录需要完成 MFA 绑定。"
                    "常见故障：提示 691 认证失败，多为密码错误或账号被锁定，先确认域账号能否正常登录邮箱，锁定需等待 30 分钟或联系服务台解锁；"
                    "提示 809 连接超时，先确认本地网络可访问外网，切换家庭宽带与手机热点重试；"
                    "提示 868 域名解析失败，切换 DNS 或更换网络后重连；"
                    "能连上但打不开内网系统，确认是企业内网地址并尝试清除浏览器代理设置。"
                    "连续三次失败请提交工单，附上错误码和截图，由服务台转网络组处理。"
                ),
            },
            {
                "title": "常见系统与域账号错误处理",
                "content": (
                    "0x80070005 访问被拒绝：当前账号对目标目录或对象没有权限，不要用管理员账号绕过，需要提交权限申请。"
                    "0x8007052E 用户名或密码不正确：确认大小写与输入法（避免全角），确认密码是否刚修改；连续输错会触发账号锁定。"
                    "0x80070533 密码已过期：按登录页提示自助重置，重置后等待 5-10 分钟再登录。"
                    "0x80070775 账号被锁定：默认 30 分钟后自动解锁，急需使用可联系服务台人工解锁。"
                    "LDAP 49 凭证无效：密码错误或过期；LDAP 32 条目不存在：用户名或工号有误；LDAP 50 权限不足：需要域管理员处理。"
                    "应用报 500：记录发生时间与操作步骤，间隔几分钟重试，持续出现请开工单并附截图。"
                ),
            },
            {
                "title": "权限申请 SOP",
                "content": (
                    "权限分三类：常规权限（共享盘只读、测试环境账号、办公系统功能权限）、高权限（读写、管理员、数据导出）、"
                    "生产环境权限（生产数据库、生产服务器、线上配置）。"
                    "常规权限：由申请人本人在 IT 服务门户提交，直属主管审批，IT 在 1 个工作日内开通。"
                    "高权限与生产环境权限：需要直属主管 + 系统负责人 + IT 安全三方审批，且必须写明用途与有效期，到期自动回收。"
                    "共享盘权限申请需提供：工号、共享盘路径或系统名称、需要的权限级别（只读/读写）、审批人、使用期限。"
                    "服务台只做流程说明与字段检查，不代替申请人提交、不直接开通权限、不临时放开高权限。"
                    "离职或转岗由 HR 触发权限回收流程，账号与权限统一在 1 个工作日内清理。"
                ),
            },
            {
                "title": "软件安装与授权流程",
                "content": (
                    "办公软件请从内网软件中心安装，不要使用来路不明的安装包或破解工具。"
                    "常规办公软件（Office、Visio、PDF 工具、输入法）可直接自助安装；"
                    "付费或授权类软件（专业设计、开发工具、数据库客户端）需要先提交软件申请，说明用途，由主管审批后再安装。"
                    "激活失败的处理：确认是否已分配授权、确认登录账号是否为域账号、确认网络可访问授权服务器；"
                    "不要使用第三方激活工具，会触发安全告警。"
                    "安装失败常见原因：安装包不完整（重新下载）、无管理员权限（需要服务台协助）、与已有版本冲突（先卸载旧版本）。"
                    "客户端反复崩溃：先更新到内网软件中心的最新版本，再收集崩溃时间点提交工单。"
                ),
            },
            {
                "title": "办公网络故障排查手册",
                "content": (
                    "第一步先判断影响范围：只有自己不通，还是同工位、同楼层、同系统的多个人都不通。多人同时不通属于网络面问题，直接升级网络组并挂高优先级。"
                    "第二步区分有线与无线：有线不通、无线正常，优先检查网口和网线；无线不通，先忘记网络重新连接，并确认是否连到了访客网络。"
                    "第三步区分内网与外网：外网正常、内网系统打不开，通常是 VPN 未连、代理设置异常或权限不足。"
                    "第四步做最小验证：换一台设备、换一个网络（手机热点）各试一次，确认是终端问题还是网络问题。"
                    "员工可自助的动作：重启网络适配器、重新获取 IP、刷新 DNS 缓存、切换浏览器代理设置、重连 VPN。"
                    "网络设备、防火墙策略、DNS 服务器、代理服务器相关的改动由网络组负责，服务台不指导用户自行修改。"
                ),
            },
            {
                "title": "账号与密码自助指南",
                "content": (
                    "密码重置：在自助密码门户使用域账号 + 已绑定手机或邮箱验证后重置，新密码需满足 12 位、包含大小写字母数字与符号，且不能与最近三次相同。"
                    "重置后 5-10 分钟内会同步到邮箱、VPN、内网系统，期间登录失败属正常，请稍后重试。"
                    "账号锁定：连续输错密码会锁定 30 分钟，等待自动解锁或联系服务台人工解锁。"
                    "MFA 令牌收不到：确认绑定手机是否变更、检查验证 App 时间是否准确、尝试改用短信验证；仍无法登录需要人工核验身份后重置 MFA。"
                    "安全提醒：任何情况下服务台都不会向你索取密码、短信验证码、MFA 一次性口令或私钥；遇到这类要求请直接怀疑是钓鱼。"
                    "新员工账号由 HR 入职流程触发创建，入职当天可使用；离职账号在离职当日停用。"
                ),
            },
            {
                "title": "资产管理与设备报修流程",
                "content": (
                    "每台公司设备都有唯一设备编号（如 PC-1024、LT-2231），报修时请提供设备编号，便于查询归属、机型与保修状态。"
                    "设备报修流程：提交报修工单 → 服务台判断是软件还是硬件问题 → 软件问题远程处理，硬件问题安排上门或送修 → 需要带走设备时提供备用机。"
                    "保修期内硬件故障由公司统一送修，个人不要自行拆机或送第三方维修，会造成保修失效。"
                    "设备借用与归还：短期项目可申请借用设备，归还时由 IT 核验外观与配置并解除账号绑定。"
                    "换机场景：设备使用满 4 年、维修成本过高或性能无法满足工作时可申请换机，需要主管审批。"
                    "离职归还：设备、电源、扩展坞、门禁卡一并交回，IT 确认后解除设备与账号的绑定关系。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇 IT 服务台文档")