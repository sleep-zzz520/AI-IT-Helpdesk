# RAG 发布评测

`backend/tests/fixtures/rag_v1.json` 包含 11 份合成知识、4 道开发题和 8 道冻结题。按问题家族拆分，开发与冻结问题没有交叉。两个分集共用带干扰文档的合成知识快照。每个用例记录环境、权限、预期行为、必要事实、支持来源及禁止项。题目与知识由 Codex 编写，定向单测验证了标注与源文档一致性，尚未经过领域专家独立复核。共享问答只读取标记为 `employee` 的文档及未标记的历史文档。显式受限文档会在两路召回和父段回填处过滤。评测根据整个知识快照独立推导员工不可见文档，核对每次裁判和生成的上下文，并保留用例级禁止来源。

## 本地复跑

在仓库根目录执行。真实评测读取当前配置中的模型链及凭据，报告仅保存模型标识和用量。每次使用新输出目录。

```bash
cd backend
../.venv/bin/python -m pytest -q tests/test_rag_release.py tests/test_rag_eval_scoring.py tests/test_evidence_gate.py tests/test_retrieval_scale.py
../.venv/bin/python -m scripts.eval_rag_release --split dev --output-dir .rag/rag-dev-local
../.venv/bin/python -m scripts.eval_rag_release --split frozen --output-dir .rag/rag-frozen-local
```

每个真实运行使用独立临时 Chroma，顺序执行，SDK 请求上限 25 秒，单阶段最多 80 秒，默认总上限 600 秒。运行器只用合成快照，经项目分块器和真实 Embedding 建索引。它复用真实问答入口，同时将知识状态隔离为 legacy 读取，覆盖检索和问答行为。Git 知识代际、在线删除限制与活动指针的回归由现有 `test_generations.py`、`test_generation_boundaries.py` 等测试覆盖。发布评测报告里的 Git commit 指代码来源，知识版本由快照内容哈希锁定。需要验收某份实际 Git 知识时，须为该快照另行标注问题、事实和证据，再将题集与可外发的脱敏快照整理为同样的 JSON 格式，使用本地参数 `--fixture PATH` 评测。手动 Actions 固定使用仓库内的合成夹具。

## 如何读报告

| 阶段 | 入口 | 读什么 |
| --- | --- | --- |
| retrieval | `retrieve()` | 按夹具显式场景过滤后的 Top 5 文档 Recall、MRR、全部证据到齐、禁止来源。无 Gold 的样本 Recall 和 MRR 不适用，仍查禁止来源。 |
| oracle | `generate_answer()` | 只对可回答题，注入正确来源并绕过检索与证据裁判，单独看事实及引用。 |
| e2e | `answer_question()` | 每跳检索、证据裁判、最终答案、实际注入片段、澄清与拒答。调用失败后的兜底按调用失败计，无法刷高拒答率。 |

检索诊断显式传入夹具场景。完整问答仍沿用产品场景白名单及后续放宽策略，两个阶段的候选范围可能不同。

以 `frozen-mail-answer` 为例，检索需要同时找回邮件放行规则和复核入口，两份文档都有各自的事实。生成时按传入顺序把文档编号为 `[证据1]`、`[证据2]`。评分器先检查回答是否写出了标注事实，再查支持该事实的文本是否真的进入提示词，最后按同句或同行的引用编号核对来源。答案提到追踪号和引用有效的证据编号，仍可能因编号指向另一份文档而失败。

评分覆盖标注事实及明确禁止的声明。它依赖用例正则和局部引用范围，开放式附加声明、同义改写和否定仍需人工读原始回答。实际回答有附加诊断或新事实时，应对照注入段落逐条复核。确定性发布报告只统计规则覆盖的事实。全论断忠实度另由下文的复核流程给出。业务证据裁判是被测链路的一部分，其判断不会写入最终评分。跨文档证据题只要求找齐不同来源，不要求模型必须分两跳找到它们。严格子段与规模退化继续看 [已有检索专项](retrieval-scale-evaluation-2026-11-02.md)。

## 发布门禁与失败处理

主 CI 已执行全量离线 pytest，包含新加入的分集校验、两路过期过滤、引用及门禁回归。手动触发 **RAG release evaluation** 可运行冻结套件。发布策略为每一条计划记录都通过，包含关键用例，任一漏题、重复、缺分、错误调用、阈值未达或超时都会返回非零。这个小套件采用逐题全通过的绝对阈值，已经通过的任一用例退化也会阻断。历史失败保留作诊断，无自动接受失败基线的步骤。Actions 只归档固定合成夹具的评测报告。

若有失败，先看 `errors` 和 `calls`，再比较同题的 retrieval、oracle 和 e2e。修改题目、快照、正则或门禁时，单独审查变更和正反例，提升对应版本并保留旧报告。冻结失败用于归因后，改动应在开发家族复现，再加独立家族确认泛化。当前小套件提供发布回归信号，真实流量质量仍需独立样本验证。

## 从差评到回归

1. 由有权限的维护者从现有 `/api/feedback/analysis` 选取差评，并在同租户核对对应消息与问答 Trace。分析中的 missing/stale 只根据是否有来源归类。用会话轮次核实匹配关系，Trace 缺版本或实际注入正文时，在记录中标为未知。
2. 在本地保留受限的追溯编号。出工作区前用虚构标识替换账号、租户、域名、地址、凭据和内部文档，删除附件和自由文本中的识别信息。只有经过复核、确认可外发的合成材料可以进入仓库或发送给模型服务。
3. 由知识维护者复核正确处置、权限、时效和证据。先为失败建立最小可复现用例，记录失败来自检索、知识、证据裁判、回答或基础设施。将同一问题家族放入同一分集，修复后保留正例和相邻反例。按快照版本发布复跑，确认通过才结案。

设计参考采用 LangSmith 的分阶段评估思路、Ragas 对事实正确性与上下文支持的区分，以及 Langfuse 对缺失结果和退化的门禁处理。使用仓库内 Python 与 pytest 运行。[LangSmith RAG 评测](https://docs.langchain.com/langsmith/evaluate-rag-tutorial) · [Ragas Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) · [Ragas Factual Correctness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/) · [Langfuse CI/CD](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd)

## 独立合成题的指标报告

`backend/tests/fixtures/rag_independent_v1.json` 与原回归集的问题家族独立，包含 3 道开发题、8 道冻结题。知识和相关性标注由 Codex 编写并核对，实际运维问题与人工金标另行建设。逐题标出相关子块；只有 `relevance_complete=true` 表示已核对整个小快照里的候选，才计算 Precision 和 nDCG。

```bash
cd backend
fixture=tests/fixtures/rag_independent_v1.json
run=.rag/rag-independent-v1-frozen
../.venv/bin/python -m scripts.eval_rag_release --fixture "$fixture" --output-dir "$run"
../.venv/bin/python -m scripts.rag_quality_metrics "$run/report.json" --fixture "$fixture"
../.venv/bin/python -m scripts.rag_claim_review "$run/report.json" "$fixture" --template > "$run/human-review.json"
```

- **排序指标**。Recall@K 按逐题子块覆盖率取宏平均，Precision@K 用固定 K 作分母，少于 K 的空位计零。MRR@K 只看第一条相关结果，nDCG@K 用二元相关性。无相关子块时，Recall、MRR 和 nDCG 不适用。全量相关性判断缺失时留空，标注完整但调用失败按未召回计。
- **上下文覆盖**。只看实际进入生成提示词的片段，对照预先标注的必要事实和对应证据断言，独立于答案有没有复述。当前使用用例级证据正则，报告名称为 `generation_context_required_facts`。它衡量标注事实覆盖，语义 Context Recall 仍需人工或已校准裁判。
- **运行开销**。按端到端单问记录时延与供应商返回的 token。分位数用 nearest-rank，建索引开销另计。只有用量完整的样本进入 token 分位数，同时报告覆盖数。小样本 P95 和 P99 只描述当次运行。
- **对照**。`rag_quality_metrics --baseline 旧报告` 要求题集、快照和分集指纹相同，按阶段列出新增失败及修复。查看两份分维度指标时也要核对模型、检索参数与评分版本。

### 全论断复核

审阅者对实际答案拆出最小可核验事实，连同模型额外补充的事实一起审。将原文片段填到 `claims[].text`，分别判定 `faithful`（实际注入片段支持）和 `correct`（与快照和题目参考口径一致）。找不到足够依据时不判通过。每个引用用 `{ "id": "1", "supports": true }` 指明是否支持该条事实。无事实的澄清或拒答记录原因。答案相关性单独按是否回应用户问题填写。宏平均、适用题数和复核覆盖率分开看。调用错误及缺评保留在分母并计零。

```bash
../.venv/bin/python -m scripts.rag_claim_review "$run/report.json" "$fixture" "$run/human-review.json"
../.venv/bin/python -m scripts.rag_claim_review "$run/report.json" "$fixture" "$run/assistant-review.json" \
  --human-reference "$run/human-review.json"
```

审阅文件与完整原始报告指纹绑定。助手初审用 `assistant_review` 标记，输出诊断分数并返回非零。人工完整审阅的状态只代表审阅覆盖通过。校准先固定同一组原文论断和引用，再比较人工与候选判定，分别报告错放和错拒。如果人工样本没有负例，无法估计错放表现。自动模型裁判和质量阈值门禁需要独立人工校准结果，目前发布工作流仍以确定性回归及权限检查为准。
