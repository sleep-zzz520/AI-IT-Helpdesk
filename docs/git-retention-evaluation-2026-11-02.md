# Git 知识库保留与清理

本轮处理旧 generation、快照、阶段缓存和部署备份的持续增长。范围是现有单机 Git Markdown 流程。清理需要停止后端；不提供在线读者租约、多机协调或零停机维护。

## 保护条件

清理先从 SQL 中读取所有来源的引用，保护在岗版本、回滚目标、最新来源意图、queued/running/prepared 任务、未知状态，以及创建或更新不足七天的任务。失败任务也不能按固定数量删除。未知 collection 不处理；共享快照按全部保留任务的引用并集判断。

缓存另做保守扫描。解析缓存保护保留快照在当前解析器下的键；切分缓存保护保留 manifest 中的文档身份和 revision；向量缓存按保留 collection 的实际子块文本，跨 profile 保护对应输入摘要。存在待处理任务或无法准确推导的历史 profile 时，暂缓全部缓存清理。保留快照损坏或丢失时停止清理，不能把解析失败视作“没有引用”。

预览不写缓存、不修改 SQL 或知识内容。执行时重新计算计划；未完成的票据阻止新一轮清理。中断后若任务集合或权威引用变化，拒绝沿用旧文件移动计划，应恢复票据并重新预览。

## 隔离、恢复与物理删除

先将候选 collection 的全部正文、metadata、向量和 HNSW 配置导出，写入带 SHA-256 的持久票据，再将 SQL 状态改为 quarantined、调用 Chroma 删除 collection、移动不再引用的快照和缓存。SQL 任务行继续保留作审计，不会把隔离任务当成可查询版本。

归档使用原始 collection，包含被 deny 规则屏蔽的文档。恢复校验块集合、正文、metadata 和检索配置；向量采用 `abs_tol=1e-7, rel_tol=1e-6`，并验证维度及有限值。原因是 Chroma 的余弦索引重新写入 float32 向量时会再次归一化，不能把合理的浮点误差当作数据丢失。恢复只还原产物和任务原状态，不修改 active/previous、来源序号或 deny。

文件恢复采用同目录临时文件、fsync、校验、原子替换，避免复制中断留下半个正式快照。隔离至少七天后才允许 `--purge` 或 `--purge-expired --apply`，执行前重新核对身份、引用和状态。删除中断可按票据继续，审计日志保留。成功恢复后清除冗余隔离副本。

本地实际发现 Chroma 1.5.9 删除逻辑 collection 后仍留下 HNSW 目录。因此物理清理不能只统计 collection 数量，也不能随意删除看似孤立的 UUID 目录。本版锁定该版本的存储结构，仅在删除前记录候选 collection 的确切 segment 归属和文件摘要；删除后关闭 Chroma 客户端，重新只读核对整个 segment 引用表，再隔离这些已知目录。未知目录、符号链接和不符合结构的目录拒绝处理；不修改 Chroma 内部 SQL。集合创建与删除接口及配置以[Chroma 官方源码](https://github.com/chroma-core/chroma/blob/main/chromadb/api/collection_configuration.py)为参考，运行验证绑定本项目固定版本。

## 部署备份

按六个文件组成的备份组保留：全量 SQL、知识表 SQL、RAG 归档、baseline、重启校验和隔离恢复校验。保护 latest 指针、七天内生成或写入的组、不完整/未验证的组，并至少保留一个早于 latest、验证成功的回退组。先同盘隔离，七天后再检查到期清除；未知文件继续保护。

成功的知识恢复证据只覆盖知识表和 RAG，不证明全量应用 SQL 已做整库恢复。备份写入、重启报告、恢复验证和清理使用同一文件锁。部署先完成备份恢复验证，再停后端执行知识清理，重启后再次验证真实双路检索和父块。维护结果和预览随 Actions artifact 保存。

## 操作与恢复

以下命令在部署目录执行。正常发布由现有 Actions 自动完成。手工维护时先停止所有后端实例，并保证其他直接访问本地 Chroma 的脚本已退出；生命周期锁与发布锁只适用于同机共享目录。

```bash
# 知识预览可在服务运行时执行；不调用模型。
docker compose exec -T backend python -m app.rag.retention

# 修改操作必须停后端；失败后也应先检查票据再启动服务。
docker compose stop backend
docker compose run --rm --no-deps -T backend python -m app.rag.retention --apply
docker compose run --rm --no-deps -T backend python -m app.rag.retention --purge-expired --apply
docker compose start backend

# 中断续做或恢复某张票据，二选一；同样要求停后端。
docker compose run --rm --no-deps -T backend python -m app.rag.retention --resume TICKET --apply
docker compose run --rm --no-deps -T backend python -m app.rag.retention --restore TICKET --apply

# 备份预览、隔离、到期清除及恢复；默认不写，--apply 执行。
python3 scripts/backup_retention.py --backup-dir backups
python3 scripts/backup_retention.py --backup-dir backups --apply
python3 scripts/backup_retention.py --backup-dir backups --purge-expired --apply
python3 scripts/backup_retention.py --backup-dir backups --restore TICKET --apply
```

不得为释放空间临时缩短保护期、修改票据时间或手动删除 segment。隔离阶段需要额外空间保存可恢复产物；空间不足时应失败停止，不能先删后备份。

## 测评范围

可复现脚本：`PYTHONPATH=backend .venv/bin/python backend/scripts/eval_retention.py`。脚本在现有 MySQL 中创建随机独立表，使用临时 Chroma 目录；结束删除自己的表和目录。输入为固定 Git 的十六篇文档和合成修改，使用确定性 1024 维向量，清理和恢复阶段禁止嵌入调用。它验证存储、引用和故障恢复，不验证模型质量；通过测试时钟推进宽限期，不等于已连续运行十四天。

原始测评记录：[retention_real.json](../backend/tests/quality_reports/retention_real.json)。本地 MySQL 8.0.43、Chroma 1.5.9 实测：六代中四代满足清理条件，在岗与回滚两代保留。首次 collection 删除后真实 SIGKILL，重新启动续做成功；归档恢复后正文、metadata、块清单和配置一致，向量最大绝对差异为 `5.960464477539063e-08`。到期清除后两代仍可双路召回各十条、结果父块核验通过；回滚目标也通过同样检查。新嵌入请求为零，独立 SQL 表和临时目录已移除。

| 文件范围 | 演练前字节 | 到期清除后字节 | 说明 |
| --- | ---: | ---: | --- |
| HNSW 文件 | 2,544,600 | 848,200 | 六代变两代，减少 66.7% |
| 快照、缓存及隔离审计 | 1,130,196 | 930,109 | 移除十六个旧文件；保留审计增加少量空间 |
| Chroma SQLite | 5,820,416 | 6,070,272 | 恢复再写后文件高水位增加；未执行 VACUUM |
| 上述全部文件 | 9,495,212 | 7,848,581 | 逻辑文件字节减少 17.3%，不等于已分配磁盘块或长期容量 SLO |

隔离阶段仍占用可恢复副本空间，七天宽限后的 purge 才释放对应文件。演练完整运行约 12.68 秒，使用本机合成修改与测试时钟，不作为生产维护时延承诺。未对不同机器、长期反复增长或断电恢复做推断。

本地完整回归：472 项后端测试通过；前端 16 项测试、lint 和构建通过；Ruff 通过。保留第三方弃用警告及两条已有 React Hook 依赖警告。

尚未覆盖：多机读者与 worker 协调、全机掉电、跨机灾难恢复、大规模长期存储测试、文档 ACL。不能将本轮结果写成回答准确率、恢复 SLO 或企业规模容量证明。
