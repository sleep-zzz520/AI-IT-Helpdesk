# 智能IT运维服务台（AI IT Helpdesk）

> 企业级 IT 运维服务台：用 Agent 自动化处理 L1 重复工单，人机协同 + 全链路可观测。

## 痛点

传统 L1 工程师处理「VPN 连不上」这类重复问题平均需要 **45 分钟人工介入**（接单 → 模板回复 → 反复追问 → 远程排查）。
本项目目标：Agent 在 **2 分钟内全自动闭环**（意图识别 → 多轮追问 → OCR 截图 → 查账号状态 → 知识库匹配 → 风险分级 → 自动执行 → 关闭工单），L1 工程师全程无感，仅在后端审计日志中看到记录。

## 核心特性（目标）

- 🤖 **Agent 自动化闭环**：LangGraph 状态机编排「意图识别 → 信息补全 → 查证 → 执行 → 收尾」
- 🛠️ **MCP 风格工具链**：标准化的 Tool 协议（`vpn.renew_certificate` 等），可插拔
- ⚖️ **风险分级执行**：低风险自动执行，高风险转人工兜底
- 🔍 **全链路 Trace**：Agent 思考过程、Tool 参数流转、Token 消耗精确可查
- 📊 **Eval 自动化评估**：合成数据 + 跑分，用数据说话

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| Agent 编排 | LangGraph | 状态机，可观测、可断点 |
| 模型 | GLM-4.7-Flash（智谱） | 免费，OpenAI 兼容协议 |
| API 层 | FastAPI | 异步、自动文档 |
| 存储 | MySQL 8 | 工单/会话/Trace |
| 前端 | 待定（React） | Trace 可视化面板 |

## 快速启动

```bash
# 1. 环境准备（首次）
bash scripts/setup_env.sh        # pyenv + Python 3.12 + .venv + 依赖

# 2. 配置环境变量
cp .env.example .env             # 填入 ZHIPU_API_KEY 与 MySQL 连接信息

# 3. 启动后端
source .venv/bin/activate
uvicorn backend.app.main:app --reload
```

> ⚠️ 开发中，快速启动说明将随进度更新。

## 目录结构

```
├── backend/            # 后端（FastAPI + LangGraph + SQLAlchemy）
│   ├── app/
│   │   ├── agents/     # Agent 状态机
│   │   ├── tools/      # MCP 风格工具
│   │   ├── api/        # HTTP 路由
│   │   ├── models/     # ORM 模型
│   │   └── services/   # 业务服务（知识库、工单）
│   └── scripts/        # 运维/验证脚本
├── scripts/            # 项目脚本（环境安装等）
└── docs/               # 文档
```

## 设计决策

> 为什么选 LangGraph 而不是 AutoGen？为什么用 openai SDK 调 GLM？…（随开发进度补充）

## 已知局限与改进方向

> 随开发进度补充
