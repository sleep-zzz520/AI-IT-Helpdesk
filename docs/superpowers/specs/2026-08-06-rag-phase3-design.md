# Phase 3 设计：咨询型知识问答路径 + 多场景验证

> 日期：2026-08-06 ｜ 状态：已批准（用户确认两项关键决策）
> 关联：ROADMAP.md P2-Phase 3（意图分流 / Agentic 多跳检索 / 运维域 judge / 多场景文档 / RAGAS 评估）

## 一、背景与目标

当前 Agent 只有一条"故障执行"路径：intent 识别场景 → 查证 → 知识库匹配 → 风险分级 → 执行。用户问"VPN 怎么配置"这类**咨询诉求**时会被当作故障处理或直接转人工，知识库（RAG）的问答价值没有被利用。

Phase 3 目标：
1. 意图分流：故障诉求走执行流程，咨询诉求走检索问答（rag_query 节点）
2. Agentic 多跳检索：检索不足时按证据实体规划下一跳，直到证据充分或达到跳数上限
3. 运维域 judge 适配：判断"检索到的方案能否回答 + 能否执行"
4. 多场景文档补全：证明"加场景 = 加文档"零代码（问答路径自动覆盖）
5. RAGAS 风格回答质量评估（faithfulness / answer_relevancy，GLM 免费做裁判）

## 二、关键决策（用户已确认）

- **场景范围**：咨询问答全场景开放；执行闭环保持 VPN 单场景（password/email/software 只开放问答）
- **多跳实现形态**：rag_query 节点内部循环（retrieve → judge → next_query → 再检索），最多 2 跳，每跳记独立 trace；不用 LangGraph 子图

## 三、架构设计

### 3.1 意图分流：intent 节点升级为二维分类

intent 节点一次 LLM 调用同时输出：

```json
{"intent": "vpn|password|email|software|other", "request_type": "troubleshoot|consult|other"}
```

- `troubleshoot`：故障诉求 → 现有执行流程（check → verify → kb → risk → execute）
- `consult`：咨询诉求 → rag_query 问答路径
- `other`（request_type）：寒暄/无关 → finalize（现状）
- 为什么不规则分流：咨询问句无错误码等硬特征（"怎么配""忘了咋办"），口语化需模型语义理解
- 为什么零额外成本：request_type 是 intent 输出的附带维度，intent ∥ extract 并行结构不变

### 3.2 图结构变更

```
parallel（intent+request_type ∥ extract）
  ├─ request_type=consult ──────────▶ rag_query ──▶ END
  │    （与场景状态无关：coming 场景也能问答，文档在知识库即答）
  ├─ request_type=troubleshoot + active 场景 ──▶ check（现有流程不变）
  ├─ request_type=troubleshoot + coming 场景 ──▶ finalize（转人工，现状不变）
  ├─ request_type=other（寒暄/无关）──▶ finalize（现状不变）
  └─ 系统异常 ──▶ handoff（现状不变）
```

- 咨询路径不经过 verify/risk/execute——纯只读问答，安全边界天然清晰
- 路由函数：`route_after_parallel` 改造，按优先级：异常 → request_type → 场景状态

### 3.3 rag_query 节点：Agentic 多跳检索（节点内循环）

```
第 1 跳：retrieve(query, scenario) 混合检索 → evidence（子块 + 父块全文，top_k=5）
  → judge（LLM 结构化 JSON）
     ├─ enough=True ──────────▶ 用 evidence 生成回答（LLM）
     └─ enough=False + next_query ──▶ 第 2 跳：retrieve(next_query, scenario)（合并证据）
         └─ 仍不足 / 达到 MAX_HOPS=2 ──▶ 用现有证据生成"部分回答 + 建议转人工"（绝不空回复）
```

- `MAX_HOPS = 2`（配置常量，可调）
- 每跳产出独立 trace：`{node: rag_query, hop: 1, query, hits: [{source, score, preview}], judge: {enough, answerable, executable, missing_info, next_query}, elapsed_ms}`
- 证据合并：跨跳 hits 去重（按 chunk id），全部进入最终回答的 evidence 上下文
- 最终 trace 含回答依据来源列表（前端展示"凭什么这么答"）

多跳的真实价值（运维域用例）：跨场景问题——"重置密码后邮箱客户端报密码错误怎么配"→ 第 1 跳命中邮箱配置，judge 发现缺少"密码重置后的注意事项"证据 → 第 2 跳补检 → 合并回答。单跳回答不完整。

### 3.4 运维域 judge 适配

judge 输出结构化 JSON：

```json
{
  "enough": true/false,        // 证据是否充分
  "answerable": true/false,    // 证据能否回答用户的问题（运维判断，非通用"证据够不够"）
  "executable": true/false,    // 方案是否需要转人工执行（区分"看完自己动手" vs "需人工操作"）
  "missing_info": ["..."],     // 还缺什么（human-readable，进回答兜底）
  "next_query": "..."          // 不足时：改写自 evidence 实体的下一跳检索词
}
```

- **next_query 防乱跳硬约束**：prompt 强制 next_query 必须改写自 evidence 中出现的实体/概念（如 evidence 提到"临时密码、首次登录修改"→ next_query 必须含这些词）；校验不通过（正则/包含检查）则 judge 作废 → 按当前证据直接回答
- judge 用 `chat_json`（response_format 强制 JSON + 容错解析）

### 3.5 多场景文档补全（证明加场景零代码）

| 场景 | 现有文档 | 补全 |
|---|---|---|
| vpn | 3 篇 | 不动 |
| password | reset-sop.md | +1 篇：账号锁定与解锁（account-lock.md） |
| email | config-guide.md | +1 篇：收发故障排查（troubleshoot.md，含 IMAP/SMTP 报错） |
| software | install-guide.md | +1 篇：许可证激活与常见报错（license-activation.md） |

- SCENARIOS 注册 email/software 为 coming 场景（intent 分类器自动识别 → 问答自动覆盖）
- 新文档保持 frontmatter 规范（scenario/risk/action/status/valid_to/source_url/tags）
- sync 后问答路径零代码覆盖新场景

### 3.6 RAGAS 风格回答质量评估

`tests/rag_qa_eval.py`，10 条合成咨询用例（含 2~3 条跨场景多跳用例），GLM 免费做裁判：

- **faithfulness（忠实性）**：LLM 把回答拆成若干声明 → 逐条对照 evidence 判断能否支撑 → 支撑比例。无需参考答案
- **answer_relevancy（相关性）**：LLM 基于回答反推 N 个假设问题 → 与原问题 embedding 余弦相似度均值。无需参考答案
- 输出：`tests/rag_qa_report.json`（与 rag_report.json / report.json 并列）

## 四、错误处理

| 场景 | 行为 |
|---|---|
| 检索异常（Chroma/BM25 失败） | 回答"知识库暂不可用，已转人工"，不 500 |
| judge 解析失败 | 跳过 judge，用当前证据直接生成回答（退化为单跳） |
| judge 返回非法 next_query | 校验不通过 → 按当前证据回答，不再多跳 |
| 回答生成失败 | safe 包装器捕获 → handoff 兜底（复用现有机制） |
| 全程兜底 | rag_query 返回"部分回答 + 建议转人工"，绝不空回复 |

## 五、前端

`SidePanel.jsx`：
- 新增 `rag_query` 渲染分支：每跳展示查询词、命中文档来源（source_url）、judge 结论（✅ 足够 / 🔄 多跳继续）
- 最终回答附"依据来源"列表
- `PENDING_LABEL` 补 `rag_query: '知识问答'`

## 六、测试与验证

1. **意图分流**：咨询 vs 故障 vs 寒暄 分类用例（3+ 条）
2. **咨询问答 e2e**：vpn 配置 / 密码重置 / 邮箱配置 / 软件安装 各 1 条 → 得到知识回答 + trace 含 rag_query 节点
3. **多跳 e2e**：跨场景问题（重置密码后邮箱报错）→ trace 显示 2 跳 + 合并证据回答
4. **回归**：vpn 故障执行链路 e2e 全通（执行路径不受影响）
5. **RAGAS 评估**：faithfulness / answer_relevancy 跑分落盘

## 七、影响面与风险

| 影响面 | 说明 |
|---|---|
| intent.py | prompt 加 request_type 维度 + 输出解析 |
| graph.py | route_after_parallel 改造 + rag_query 节点注册 |
| 新增 agents/nodes/rag_query.py | 多跳检索循环 + judge + 回答生成 |
| scenarios.py | 注册 email/software（coming） |
| docs/knowledge/ | +3 篇文档 |
| SidePanel.jsx | rag_query 渲染分支 |
| tests/rag_qa_eval.py | 新增评估 |

风险：
- judge 免费模型可能输出不稳定 → 校验 + 退化为单跳兜底
- 多跳增加 LLM 调用（最多 2 judge + 1 回答）→ 咨询路径耗时可控（< 3 次调用，均有模型链故障转移）
- 不改动执行路径 → 回归风险低
