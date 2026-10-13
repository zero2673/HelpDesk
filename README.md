# HelpDesk · 企业内部 IT 服务台多 Agent 系统

把公司 IT 服务台的一线分流做成可运行的多 Agent 系统：员工的设备故障、软件安装、网络与 VPN、账号密码、权限申请，先由意图识别和路由决定谁来处理，能自助解决的直接给步骤，需要动后台的转工单并交接上下文。

## 能力一览

- **细粒度意图识别**：LLM + Embedding + 关键词三路融合，7 类 IT 意图（设备故障 / 软件安装 / 网络问题 / 权限申请 / 账号密码 / 系统报错 / 转工单）
- **多 Agent 编排**：服务台接待、终端与系统支持、网络与连通性、账号与权限、工单升级五个 Agent，支持主 Agent + 辅助 Agent 协作处理复合问题
- **意图驱动 RAG**：查询改写 → 并行召回 → 合并去重 → LLM 重排；知识库为 IT 操作手册、VPN 教程、常见错误处理、权限申请 SOP
- **分层记忆**：Redis 存工作记忆与会话摘要，ChromaDB 存情景记忆与用户画像，超阈值自动压缩
- **动态 Skills**：把服务台规范（接待、终端、网络、权限、工单）按 Agent 类型与关键词动态注入 system prompt，改完热加载即可生效
- **工具层**：错误码查询（HTTP / Windows AD / VPN）、诊断计划、资产查询、权限申请字段校验、工单创建；带缓存、熔断、降级与结果重排
- **在线监控与降权**：按 Agent 成功率与延迟计算降权系数，表现差的 Agent 自动被路由绕开
- **端到端评测**：内置 IT 场景评测集，LLM-as-Judge 四维打分 + 回归基线

## 目录结构

```text
api/main.py                    FastAPI 入口（/chat、/search、/knowledge/*、/skills、/monitor、/eval/run）
agents/agent_orchestrator.py   多 Agent 编排、路由与主辅协作
agents/tools.py                Agent 工具定义与实现
core/intent_recognizer.py      三路融合意图识别与实体抽取
core/skill_loader.py           动态 Skills 加载
mcp/tool_manager.py            工具层：缓存、熔断、重排
mcp/knowledge_base.py          ChromaDB 知识库
memory/conversation_memory.py  Redis + ChromaDB 分层记忆
monitor/performance_monitor.py 在线监控与降权
evaluation/evaluator.py        端到端评测
skills/                        业务规范（5 个 IT 场景 Skill）
tests/                         单元测试（17 项）
```

## 快速开始

```bash
# 依赖：Python 3.11、Redis、ChromaDB
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 配置 .env
ANTHROPIC_API_KEY=...
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
REDIS_URL=redis://:password@127.0.0.1:6379/1
CHROMA_HOST=127.0.0.1
CHROMA_PORT=8001

# 启动
python -m uvicorn api.main:app --port 8000     # Swagger: http://localhost:8000/docs
chroma run --path ./data/chroma --port 8001    # 向量库
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/chat` | 主对话：记忆读取 → 意图识别 → 路由 → 执行 → 记忆写入 |
| POST | `/search` | 知识库检索：改写 + 并行召回 + 重排 |
| POST | `/knowledge/add`、`/knowledge/upload` | 导入知识库（JSON 数组或文本/Markdown 文件） |
| GET | `/skills`、POST `/skills/reload` | 查看 / 热加载业务规范 |
| GET | `/monitor`、`/metrics` | 在线指标、降权系数与 Prometheus 指标 |
| POST | `/eval/run` | 端到端评测（意图准确率 + LLM-as-Judge + 回归检测） |
| GET | `/trace/tools`、`/trace/tool/{request_id}` | 工具调用轨迹 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q      # 17 passed
```

## 说明

- 工单系统与资产系统为示例实现（`create_ticket` / `lookup_asset`），接口契约已固定，接入真实系统时替换函数体即可
- 服务台只做一线分流与信息整理：涉及权限变更、生产环境、数据恢复的操作一律走审批或转二线，不代替执行
- 生产部署：`Dockerfile` + `docker-compose.yml` 提供 Redis / ChromaDB / Prometheus / Nginx 的编排
