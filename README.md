# 智能IT运维服务台（AI IT Helpdesk）

[![CI](https://github.com/sleep-zzz520/AI-IT-Helpdesk/actions/workflows/ci.yml/badge.svg)](https://github.com/sleep-zzz520/AI-IT-Helpdesk/actions/workflows/ci.yml)

> 企业级 IT 运维服务台：用 **LangGraph 状态机** 编排 Agent，将 L1 重复工单（如 VPN 证书续期）从「45 分钟人工介入」压缩到「**2 分钟全自动闭环**」。

**一句话卖点**：不是"能聊天的 Agent demo"，而是一套**可观测、可控、可评估、能上线**的 Agent 工程系统——风险分级执行 + 全链路 Trace + RAG 混合检索 + Eval 跑分 + 多租户安全 + CI/CD 测试工程化。

## 痛点与解法

**传统 L1 运维**困于"人肉中转"：接单 → 模板回复 → 反复追问 → 远程排查，一个简单故障耗时 45 分钟。

**本项目的 Agent 模式**（感知 → 决策 → 执行 全自动闭环）：

```
用户:"VPN连不上，报错Error 800，设备是Windows 11"
  → ①意图识别(vpn) → ②信息抽取 → ③查证(证书已过期)
  → ④知识库匹配(续期方案) → ⑤风险分级(低风险→自动)
  → ⑥执行续期 → ⑦自动回复+关单+记 Trace
```

L1 工程师全程无感，仅在后台审计日志看到一条成功记录。

## 核心特性

- 🤖 **确定性 Agent 编排**：LangGraph 状态机（节点 + 条件边），流程显式、可观测、可断点
- ⚖️ **风险分级执行**：信任边界（低风险自动、高风险转人工，`RISK_POLICY` 配置）
- 🛠️ **MCP 风格工具链**：查询/执行工具按名字注册（`QUERY_REGISTRY` / `TOOL_REGISTRY`），可插拔；执行类工具是信任边界保护对象（只有过风险分级才允许）
- 📊 **全链路 Trace + 实时推送**：每节点语义化摘要（AI/规则/工具分类 + 状态色）+ 全链路耗时 + **SSE 边跑边推**（前端实时点亮，不"干等"）
- 🖼️ **视觉 OCR**：GLM-4V 从报错截图识别错误码（前端支持粘贴/上传）
- 📈 **Eval 自动化评估**：意图 16/16、RAG Recall@5=1.0、回答 faithfulness=0.905、转译 100%（四份报告落盘 tests/，CI 可自动跑）
- 🧩 **场景注册表（数据驱动）**：加新场景 = 填一张表，图代码零改动；启动校验防遗漏
- 🔎 **RAG 混合检索**：BM25（jieba 分词）+ 向量双路召回 → RRF 融合 → Rerank 精排（可选）
- 🧠 **Agentic 多跳检索**：retrieve → judge → 证据不足自动规划下一跳（MAX_HOPS=2，每跳记 Trace）
- 📄 **多模态知识入库**：pdf/docx/pptx/xlsx/图片/音视频 → GLM-4V/GLM-ASR 转译降维（8 类格式统一文本入库）
- 🔄 **蓝绿发布**：双索引交替，全量替换零中断 + 秒级回滚；影子测试对比新旧索引 Recall
- 👍 **反馈闭环**：用户 👍/👎 → 负反馈自动归类（文档缺失 vs 文档过时），反哺知识库运营
- 🏷️ **工单流转状态机**：新建 → 处理中 → 已解决/转人工，显式转移表 + 护栏（非法流转被挡）+ 流转历史留痕
- 🏢 **多租户 + 权限 + 审计**：PBKDF2 密码哈希 + HMAC token + 登录态注入身份（不信任前端传值）；跨租户数据隔离；登录/会话/反馈/知识库操作全审计
- 🔌 **真实监控 API 对接**：`MONITOR_MODE=mock|real` 双模式，real 走 HTTP（超时/重试/错误归一化），换真实系统只改 `MONITOR_BASE_URL`
- 🧪 **测试工程化**：pytest 一条命令跑全量离线回归（mock LLM + 内存 SQLite），CI 自动验证，改坏流程立刻红

## 架构图

```
┌───────────────────────────────────────────────────────────────┐
│  前端（React + Tailwind）                                       │
│  对话面板 · 工单状态卡 · Trace 执行链路 · 知识库管理页（台账/     │
│  同步/蓝绿切换/检索调试/反馈分析）                                │
└──────────────────────┬────────────────────────────────────────┘
                       │ /api/*（同源：vite proxy / nginx 反代）
┌──────────────────────▼────────────────────────────────────────┐
│  FastAPI 网关                                                  │
│  会话/消息/Trace · 反馈闭环（👍👎 + 负反馈分析）· 知识库管理       │
└──────────────────────┬────────────────────────────────────────┘
                       ▼
┌───────────────────────────────────────────────────────────────┐
│  LangGraph 状态机（7 节点 + 4 条条件边 + 异常兜底）               │
│                                                               │
│  intent ─▶ extract ─▶ check ─┬─缺信息─▶ ask（追问）             │
│                              └─齐全─▶ verify ─▶ kb             │
│  kb ─▶ risk ─┬─auto─▶ execute ─┬─成功─▶ close（收尾）          │
│               └─human─▶ handoff │ └─失败─┘                     │
│  （safe 包装器：LLM/工具异常 → error → handoff，绝不 500）       │
│  咨询意图 → rag_query（多跳检索问答，见下方 RAG 层）              │
└──────┬───────────┬───────────┬───────────┬────────────────────┘
       ▼           ▼           ▼           ▼
   GLM-4.7-Flash   mock 监控   执行 Tool   RAG 知识库
   意图/抽取/OCR    API        （注册表）   ┌─────────────────────┐
       │           │           │          │ 混合检索（RRF 融合）  │
       └───────────┴─────┬─────┘          │ 向量路 + BM25 路     │
                         │                │  → Rerank（可选）    │
                         ▼                │ Agentic 多跳         │
                   MySQL                  │ 蓝绿双索引（零中断）  │
             会话/消息/Trace               │ 台账 kb_documents    │
             知识库台账 kb_documents       │ 转译降维（8 类格式）  │
             反馈 feedback                 └─────────────────────┘
```

## RAG 知识库

### 架构：文档 → 分块 → 嵌入 → 双路检索 → 多跳问答

```
docs/knowledge/**（md/pdf/docx/pptx/xlsx/图片/音视频，frontmatter 元数据）
        │  sync 引擎（reconcile：增/改/删/跳过，幂等 + 失败自愈）
        ▼
  父子块分块（小块检索、父块生成） → 智谱 Embedding-3（1024 维）
        ▼
  蓝绿双 Chroma collection（active 在岗 / candidate 候选，交替发布）
        │  检索时
        ▼
  query ──增强（错误码提取）──▶ 向量路（语义）┐
        │                        BM25 路（词法）┴─ RRF 融合 ── Rerank（可选）
        ▼                                          │
  judge（enough/answerable/executable/missing_info）──不足──▶ 规划下一跳（≤2）
        ▼
  GLM 依据证据生成回答（Trace 记录 sources，供前端展示与反馈归类）
```

### 知识库生命周期管理

- **台账 = 事实账本**：`kb_documents` 表记录每篇文档的 hash/chunk 数/有效期/变更记录；列表、过期预警、ChangeLog 全查 MySQL，不碰向量库与源文件
- **reconcile 增量引擎**：内容 hash 变化才重建（幂等：连续两次 sync 第二次全跳过）；软删除（status=inactive）/ 物理删除（置 removed 留审计痕迹）/ 复活
- **蓝绿发布**：sync 全量写入候选索引（线上零中断）→ 影子测试对比新旧 Recall → 切指针秒级生效；回滚 = 再切一次（旧库未被覆盖前有效）
- **过期机制**：frontmatter `valid_to` 过期即不检索（检索过滤 `$gte` 今天）；管理页提前 30 天预警
- **反馈闭环**：用户 👍/👎 → 后台按该轮回答的 Trace（rag_query.sources）自动归类：无命中 → 文档缺失（补文档）；有命中 → 文档过时（修文档）

### 规模边界

| 规模 | 方案 | 说明 |
|---|---|---|
| 千级～十万级 chunk | ChromaDB（当前） | 单机毫秒级，持久化目录挂卷即可 |
| 百万级 chunk | Milvus | 只需改 `create_store()` 工厂函数（VectorStore 抽象层已隔离，检索/同步/切分代码零改动） |

多模态演进：当前用 GLM-4V/GLM-ASR **转译降维**（运维截图语义 95% 在文字，成本极低）；chunk 保留 `media_ref`（原始文件 + 页码），升级 bge-visualized-m3 / ColPali 联合嵌入时可直接路由原图。

## 快速启动

### 方式一：Docker 一键部署（推荐）

```bash
docker compose up -d --build
# 浏览器打开 http://localhost:8080
```

> 国内网络已内置镜像加速源；`ZHIPU_API_KEY` / `MYSQL_PASSWORD` 从项目根 `.env` 自动读取。

### 方式二：本地开发（前后端分离）

```bash
# 终端 1：后端（首次需先建表，uvicorn 启动会自动建）
cd backend
python3 -m venv .venv && source .venv/bin/activate   # 首次
pip install -r requirements.txt                       # 首次
uvicorn app.main:app --reload                         # http://127.0.0.1:8000

# 终端 2：前端（vite proxy 自动转发 /api 到后端）
cd frontend
npm install                                           # 首次
npm run dev                                           # http://localhost:5173
```

### 演示路径

1. **登录**（多租户/权限体系）：用演示账号一键填充登录
   - `admin / Admin@2025`：管理员（可见知识库管理 + 审计日志）
   - `zhangsan / Zhangsan@2025`：普通用户（Acme 集团）
   - `lisi / Lisi@2025`：普通用户（Globex 科技）→ 与 zhangsan 数据隔离
2. 登录后自动开新会话（工单状态：新建），输入 `VPN连不上，报错Error 800，设备是Windows 11`（或截图后 Cmd+V 粘贴）→ 工单进入「处理中」
3. 右侧实时观察：**意图识别 → 查证 → 风险分级 → 执行续期 → 收尾关单** 的 Trace 链路 → 工单变「已解决」+ 工单号；工单卡片下方可见状态流转历史（新建→处理中→已解决）
4. 回复 `未解决` → 工单重新打开（处理中）——状态机闭环
5. 输入 `你好` / `我的密码忘了` → Agent 正确识别并转人工（不误触业务流）
6. 输入 `VPN证书怎么续期` → 走 **知识问答**（rag_query 多跳检索，Trace 显示依据来源）
7. **多租户隔离演示**：退出登录，用 lisi 登录 → 看不到 zhangsan 的会话（跨租户 404）
8. 用 admin 登录 → 顶栏「知识库管理」（台账/检索调试/同步/切换）+「审计日志」（谁在何时做了什么）

### 知识库同步（首次部署后执行一次）

```bash
cd backend && source ../.venv/bin/activate
python -m app.rag.sync                    # 全量写入候选索引
python -m scripts.test_shadow             # 影子测试：新旧索引 Recall 对比
curl -X POST http://localhost:8000/api/kb/switch   # 切换生效（零中断）
```

## Eval 跑分（真实数据，不伪造）

```bash
cd backend && source ../.venv/bin/activate
python -m tests.eval            # 意图识别（16 条）
python -m tests.rag_eval        # 混合检索 Recall@k / MRR（10 条）
python -m tests.qa_eval         # 回答质量 faithfulness / answer_relevancy（RAGAS 风格）
python -m tests.transcribe_eval # 多模态转译质量（10 张合成截图）
```

```
=== 意图识别 Eval（16 条合成用例，含 request_type 维度）===
准确率 : 16/16 = 100%    GLM 调用: 16 次

=== RAG 检索 Eval（10 条运维用例）===
[hybrid] Recall@1=0.6 Recall@3=0.9 Recall@5=1.0 MRR=0.758

=== 回答质量 Eval（GLM 做裁判）===
faithfulness=0.905 / answer_relevancy=0.777
```

用例设计：正例变体（测泛化）+ 密码/邮箱/软件场景 + 干扰项（测不误判）+ 边界（询问≠故障）；全部报告落盘 `tests/*.json`（含影子测试 `shadow_report.json`）。

## 运行测试

**一条命令跑全量离线回归**（不依赖真实 GLM、不连 MySQL）：

```bash
cd backend && source ../.venv/bin/activate
pip install -r requirements.txt   # 首次：含 pytest
pytest                            # 离线单测（mock LLM + 内存 SQLite + mock 监控）
```

- **离线单测**（`tests/test_*.py`，pytest）：Agent 故障执行链路（mock LLM + mock 监控）、咨询问答路径（mock 检索）、工单状态机（纯逻辑）、会话持久化（内存 SQLite）。conftest 在 import 前设 `MONITOR_MODE=mock` 等，回归零外部依赖。
- **真实 Eval**（`tests/*_eval.py`）：需要 `ZHIPU_API_KEY` + MySQL，验证模型真实能力，见上节（也可在 CI 手动触发 eval.yml）。
- CI 里 `pytest` 是必跑步骤，任何 commit 都会自动验证，改坏流程立刻红。

## CI/CD（GitHub Actions）

**分层设计**：把「工程可信度」和「真实 Eval」分开，主流程不因外部依赖而红。

| 工作流 | 触发 | 依赖 | 跑什么 |
|---|---|---|---|
| [CI](.github/workflows/ci.yml) | push / PR | 零密钥、零数据库 | 后端语法检查 + import 冒烟 + **pytest 离线单测**（mock LLM + 内存 DB）；前端 `npm ci` → lint → build |
| [Eval](.github/workflows/eval.yml) | 手动触发 | `ZHIPU_API_KEY` + MySQL | 四份 Eval（intent / rag / qa / transcribe）+ 报告上传 |

- **CI**：任何 commit 都自动验证「工程可信度」（改坏一个 import / 前端 build 挂了立刻红），不依赖真实模型，免费稳定。
- **Eval**：四份真实 Eval 依赖 GLM（免费但限流）+ MySQL，故走 `workflow_dispatch` 手动触发；需在仓库 `Settings → Secrets` 配置 `ZHIPU_API_KEY`。跑完上传四份 `*_report.json`，可用于复盘和 README 贴分。
- 为什么分开：真实 Eval 每次跑都要花钱/等限流，混进主流程会让 CI 频繁红，违背"每次提交都安心"的初衷。

## 设计决策

| 决策 | 为什么 |
|---|---|
| **LangGraph 而非 AutoGen** | 本项目是「可预期、可观测」的确定性流程（意图→追问→查证→执行），LangGraph 的节点+条件边=显式状态机，Trace 可视化、断点调试天然支持；AutoGen 偏多智能体对话编排，流程不可预期。手写循环则逻辑散落、难维护 |
| **场景注册表（数据驱动）** | 加场景 = 填一张表（信息清单/意图规则/查证工具/话术），图代码零改动；启动校验防配置遗漏 |
| **风险分级 = 信任边界** | 企业让 AI 落地的第一问是"它凭什么能自己动手"。`RISK_POLICY` 划清"能自动的/要人点头的"，未知风险默认 high（安全优先） |
| **mock 先行** | 查询/执行工具先 mock（返回格式即真实 API 契约），换真实实现只改 tools/ 一个文件，节点代码零改动 |
| **openai SDK 调智谱** | 模型无关：换 DeepSeek/OpenAI 只改 `.env` 的 base_url 和 model |
| **视觉模型做 OCR** | UI 截图的错误码藏在弹窗里，视觉模型"看图理解"优于传统 OCR"认字符"，且零系统依赖 |
| **同源代理** | 前端永远用相对路径 `/api`：开发 vite proxy、生产 nginx 反代，一套代码无 CORS |
| **台账 + 向量库分离** | 知识库生命周期（变更检测/过期/审计）全走 MySQL 台账，向量库只是索引——读台账毫秒级，且不怕 Chroma 重建 |
| **蓝绿双索引发布** | 同步永远写候选索引（线上零中断），影子测试验证后切指针（秒级生效/回滚）；"切换"只是改一个落盘文件，无数据搬迁 |
| **转译降维而非联合嵌入** | 运维截图语义 95% 在文字：GLM-4V/GLM-ASR 转成文本入库（免费/毫秒级），保留 media_ref 兼容未来 ColPali |
| **多模型故障转移（failover）** | 模型链按优先级排列 + 游标记忆 + 429/5xx 短拉黑 + 403/404 账号级拉黑——免费模型限流时自动切换下一个，不再白等退避 |
| **SSE 流式推送执行链路** | 用户从"干等回复"变"实时看 Agent 每步在干嘛"；边跑边落库防断连丢数据（nginx 关缓冲解决 2s 缓冲踩坑） |
| **登录态注入身份（不信任前端）** | 多租户隔离靠登录态（token → user_id → tenant_id），前端传值一律不采纳——跨租户访问直接 404 |
| **测试工程化（mock LLM + 内存 DB）** | 回归零真实 GLM、零 MySQL：conftest 在 import 前锁环境变量 + patch 5 个 LLM 调用点 + SQLite 内存库——CI 稳定免费不依赖外部 |

## 目录结构

```
├── backend/
│   ├── app/
│   │   ├── agents/       # LangGraph 状态机（scenarios 注册表 + nodes/）
│   │   ├── tools/        # MCP 风格工具（查询/执行注册表 + OCR + 知识库）
│   │   ├── rag/          # RAG 子系统（loader/splitter/embedder/store/bm25/
│   │   │                 #   reranker/retriever/sync/transcribe/asr/video）
│   │   ├── api/          # FastAPI 路由（conversations / kb 管理 / feedback）
│   │   ├── services/     # 会话持久化（存取 state）
│   │   └── models.py     # ORM（conversations/messages/traces/kb_documents）
│   ├── scripts/          # 验证脚本（e2e/sync 闭环/影子测试/OCR/转译…）
│   ├── tests/            # pytest 离线单测（test_*.py）+ Eval 测试集与评估器
│   ├── conftest.py       # pytest 全局配置（mock LLM + 内存 DB + 隔离 KB）
│   └── pytest.ini        # pytest 配置（pythonpath/testpaths/markers）
├── frontend/             # React + Tailwind（对话/工单/Trace/知识库管理）
├── docs/knowledge/       # 知识文档源（唯一事实来源，sync 扫描它）
├── scripts/              # 环境安装脚本
└── docker-compose.yml    # 一键部署
```

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph（状态机，非 AutoGen——见设计决策）|
| 模型 | GLM-4.7-Flash（免费，多模型 failover）/ GLM-4V-Flash（OCR）/ Embedding-3（0.5 元/百万 tokens）|
| 向量库 | ChromaDB（VectorStore 抽象，预留 Milvus）|
| API | FastAPI |
| 存储 | MySQL 8 + SQLAlchemy 2.0 |
| 检索 | BM25（jieba + rank_bm25）⊕ 向量 → RRF 融合 → 智谱 Rerank（可选）|
| 前端 | React + Vite + Tailwind v4 + Phosphor（对话/工单/Trace/知识库管理）|
| 测试 | pytest（mock LLM + 内存 SQLite，离线回归）+ 四份真实 Eval |
| CI/CD | GitHub Actions（主 CI 零密钥自动验证 + Eval 手动触发）|
| 部署 | Docker Compose（nginx 反代 + mock_monitor 监控服务）|

## 已知局限与改进方向

- **图片不持久化**：截图 base64 仅当前会话可见（历史存 `[图片]` 标记），后续可接对象存储
- **免费模型限流**：GLM-4.7-Flash 有频率限制（已加自动重试 + failover），生产建议付费模型 + 更完善的重试/降级
- **ChromaDB 单目录单进程**：同一 persist_dir 只支持一个进程访问（官方限制），多 worker/多实例部署需切远端模式或按实例分目录
- **转人工闭环未打通**：`handoff` 目前是终态（转人工后无"人工接管→处理→回写→关单"链路），真实服务台核心能力，见改进方向
- **长对话压缩/摘要**：`messages` 无限累积，`extract` 每轮读全量历史，成本随轮次上涨；需对话压缩
- **监控数据为合成数据**：`mock_monitor` 模拟企业 VPN 证书系统，`MONITOR_MODE=real` 已预留 HTTP 对接，换真实系统只改 `MONITOR_BASE_URL`
- **表结构迁移**：目前 `create_all` + 增量补列，生产需引入 Alembic
- **反馈分析是规则版**：按 Trace 命中情况归类（缺失/过时），可升级为 LLM 二次归类（成本换精度）
- **Token 无刷新/无登出失效**：HMAC token 24h 过期，生产需刷新机制
