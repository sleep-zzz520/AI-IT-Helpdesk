# 智能IT运维服务台（AI IT Helpdesk）

> 企业级 IT 运维服务台：用 **LangGraph 状态机** 编排 Agent，将 L1 重复工单（如 VPN 证书续期）从「45 分钟人工介入」压缩到「**2 分钟全自动闭环**」。

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

- 🤖 **确定性 Agent 编排**：LangGraph 状态机（7 节点 + 条件边），流程显式、可观测、可断点
- ⚖️ **风险分级执行**：信任边界（低风险自动、高风险转人工，`RISK_POLICY` 配置）
- 🛠️ **MCP 风格工具链**：查询/执行工具按名字注册（`QUERY_REGISTRY` / `TOOL_REGISTRY`），可插拔
- 📊 **全链路 Trace**：每节点语义化摘要（AI/规则/工具分类 + 状态色），前端可视化
- 🖼️ **视觉 OCR**：GLM-4V 从报错截图识别错误码（前端支持粘贴/上传）
- 📈 **Eval 自动化评估**：12 条合成用例，意图识别 **100%**（`tests/report.json`）
- 🧩 **场景注册表（数据驱动）**：加新场景 = 填一张表，图代码零改动

## 架构图

```
┌─────────────────────────────────────────────────────────┐
│  前端（React + Tailwind）                                 │
│  对话面板 · 工单状态卡 · Trace 执行链路可视化               │
└──────────────────────┬──────────────────────────────────┘
                       │ /api/*（同源：vite proxy / nginx 反代）
┌──────────────────────▼──────────────────────────────────┐
│  FastAPI 网关（会话 / 消息 / Trace 接口）                  │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────┐
│  LangGraph 状态机（7 节点 + 4 条条件边 + 异常兜底）         │
│                                                         │
│  intent ─▶ extract ─▶ check ─┬─缺信息─▶ ask（追问）       │
│                              └─齐全─▶ verify ─▶ kb       │
│  kb ─▶ risk ─┬─auto─▶ execute ─┬─成功─▶ close（收尾）    │
│               └─human─▶ handoff │ └─失败─┘               │
│  （safe 包装器：LLM/工具异常 → error → handoff，绝不 500） │
└──────┬───────────┬───────────┬───────────┬──────────────┘
       ▼           ▼           ▼           ▼
   GLM-4.7-Flash   mock 监控   知识库       执行 Tool
   意图/抽取/OCR    API        方案表       （注册表）
       │           │           │           │
       └───────────┴─────┬─────┴───────────┘
                         ▼
                   MySQL（会话 / 消息 / Trace 持久化）
```

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

1. 打开页面（自动创建会话）
2. 输入 `VPN连不上，报错Error 800，设备是Windows 11`（或截图后 Cmd+V 粘贴）
3. 右侧实时观察：**意图识别 → 查证 → 风险分级 → 执行续期 → 收尾关单** 的 Trace 链路
4. 输入 `你好` / `我的密码忘了` → Agent 正确识别并转人工（不误触业务流）

## Eval 跑分（真实数据，不伪造）

```bash
cd backend && source ../.venv/bin/activate && python -m tests.eval
```

```
=== 意图识别 Eval（12 条合成用例）===
准确率 : 12/12 = 100%    GLM 调用: 12 次
```

用例设计：正例变体（测泛化）×5 + 密码场景 ×2 + 干扰项（测不误判）×3 + 边界（询问≠故障）×2。

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

## 目录结构

```
├── backend/
│   ├── app/
│   │   ├── agents/       # LangGraph 状态机（scenarios 注册表 + nodes/）
│   │   ├── tools/        # MCP 风格工具（查询/执行注册表 + OCR + 知识库）
│   │   ├── api/          # FastAPI 路由
│   │   ├── services/     # 会话持久化（存取 state）
│   │   └── models.py     # ORM 三表（conversations/messages/traces）
│   └── tests/            # Eval 测试集与评估器
├── frontend/             # React + Tailwind（对话/工单/Trace 面板）
├── scripts/              # 环境安装脚本
└── docker-compose.yml    # 一键部署
```

## 技术栈

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph |
| 模型 | GLM-4.7-Flash（免费）/ GLM-4V-Flash（OCR）|
| API | FastAPI |
| 存储 | MySQL 8 + SQLAlchemy 2.0 |
| 前端 | React + Vite + Tailwind v4 + Phosphor |
| 部署 | Docker Compose（nginx 反代）|

## 已知局限与改进方向

- **图片不持久化**：截图 base64 仅当前会话可见（历史存 `[图片]` 标记），后续可接对象存储
- **免费模型限流**：GLM-4.7-Flash 有频率限制（已加自动重试），生产建议付费模型 + 更完善的重试/降级
- **确定性流程的边界**：复杂长尾场景（非结构化诉求）需要 RAG 检索 + 更灵活编排
- **无认证/多租户**：当前单用户演示，生产需接入身份体系
- **mock 数据**：监控 API 与账号数据为合成数据，需对接真实系统
- **表结构迁移**：目前 `create_all` 建表，生产需引入 Alembic 迁移
