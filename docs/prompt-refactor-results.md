# 提示词优化交付记录

日期：2026-11-07。模板版本：`2026-11-07.1`。已部署到六服务 MVP 环境。

## 最终改动

逐节点按“做什么、需要知道什么、交付什么”重组，覆盖默认问数、辅助调用及显式兼容路径。没有新增 Agent、服务或提示词平台。

| 处理 | 落地位置与结果 |
|---|---|
| 删除身份宣言、重复程序边界、过时预算与拆步教程 | 默认短模板集中到 [prompts.py](D:/Develop/semora/services/agent/semora_agent/prompts.py)；三种 Reviewer 各有任务模板。 |
| 字段含义只保留一个定义来源 | QueryIntent、澄清、Review 字段说明进入 Pydantic Schema；Reviewer 读取当前用到的意图字段定义，因为它看不到 Analyst 工具的输入 Schema。 |
| 当前事实通过结构输入传递 | SQL 后来源补齐、再汇总、完整性保护及最终排序/限量明确区分。由计划和执行器生成当前适用事实，不发送所有能力教程。 |
| 知识正文和获取过程分离 | Analyst / Reviewer 收到正文、引用标识、定义状态、适用范围与冲突；知识库、文档、分块、位置和版本留在程序映射。原始证据不被模型摘要替代。 |
| 程序规则留在程序 | 工具白名单、预算、权限、SQL 门禁、审批绑定、迟到结果丢弃仍由原代码强制。完整 TaskContract 保存于私有状态，模型只接收任务简要信息。 |
| 辅助调用按任务收敛 | 知识研究仍保留真正需要的检索/阅读/溯源协议；动作 Schema 随可用动作收窄并本地校验。本体候选按候选 Schema 与原有证据规则校验。 |
| 兼容提示词独立 | [legacy_prompts.py](D:/Develop/semora/services/agent/semora_agent/legacy_prompts.py) 保留原调用协议，同源依赖可用，不再向默认 Reviewer 注入旧规划段。 |
| 输出失败不丢有效 SQL | SQL/结果说明有明确输出字段和本地验证；说明生成失败时保留实际 SQL/结果，说明不可用，不把合法查询转成失败。 |

没有改变模型配置、思考开关、请求超时、修订预算、查询能力或源库访问权限。

## 完整请求对比

同一组合成 SQLite 请求、同一结果，比较四个阶段的首次模型请求。字节数使用同样的 JSON 序列化口径，包含消息和工具 Schema；不是供应商计费 token，也不代表所有业务场景的缩减率。

| 阶段 | system 字符（旧 → 新） | 完整请求字节（旧 → 新） | 请求缩减 |
|---|---:|---:|---:|
| Analyst 意图理解 | 1770 → 325 | 16,542 → 14,238 | 13.9% |
| Reviewer 方案预检 | 4244 → 156 | 24,525 → 7,117 | 71.0% |
| Reviewer 执行前复核 | 4420 → 230 | 30,870 → 20,383 | 34.0% |
| Reviewer 结果复核 | 4420 → 143 | 30,356 → 19,623 | 35.4% |

四个阶段合计 102,293 → 61,361 字节，减少 40.0%。保留了实体选取、记录范围、存在性和最终排序等必要语义；没有为了缩短 system 而删除这些定义。

基线和结果：`artifacts/prompt-refactor/before.json`、`after.json`。复现：

```powershell
.\.venv\Scripts\python.exe scripts/evaluation/prompt_snapshot.py --output artifacts/prompt-refactor/current.json
```

## 验证与修正过程

首轮精简后的真实模型验收没有通过，未直接部署。保留了失败记录并修正以下必要信息：

1. `selection.object` 表示被选取对象，不能被时间属性所属记录替代。字段定义与任务判断均明确实体/记录的区别。
2. 方案预检只核对业务意图；移除该阶段不需要的物理步骤和最终 SQL 检查资料。
3. Review 的问题摘要是字符串，具体问题对象属于 findings；Schema 明确说明两者。
4. 分步执行既可能补齐来源列，也可能随后再汇总；两种操作按真实顺序同时传入，不能只保留其中一种说明。
5. 每条 SQL 的数据源范围、SQL 后补齐列与局部聚合明确标识；正确性检查不要求局部 SQL 重复最终业务粒度，性能建议不作为错误结果。SQL 行数上限与业务 Top N 分开。执行器编译产物记录真实接受行数、截断/溢出处理及检查时机；Reviewer 按完整链路判断，安全停止限制不等同错误结果。

上述调整均为任务所需语义或执行事实，没有恢复身份宣言、行业词汇和旧拆步教程。失败尝试留在 `artifacts/prompt-refactor/*.attempt*.json`，其中部署后重复验证的失败另见 `regroup-deployed-real-model.json`；补齐源边界和局部聚合含义后，最终两次重复通过，用于说明本轮并非首次改动就全部通过。

最终自动化：API **465 通过、1 跳过**；Wren **12 通过**。另通过依赖锁定检查、本地临时 JSONB 往返（事务回滚）、两个历史成功任务只读回看及审计校验。

新增测试验证：知识公式/例外/精度/冲突与状态不丢失、引用稳定且可恢复、当前意图定义交接、阶段输入边界、来源补齐与再汇总的顺序、执行完整性保护事实，以及说明输出无效时查询仍能完成。

真实模型 + 真实 Wren + 本地合成数据库验收：

| 场景 | 预期 | 已通过记录 |
|---|---|---|
| 选最后两个不同实体，再检查它们全部记录是否无匹配 | 只返回实体 2 | `existence-repair-real-model.json`、`existence-repeat-real-model.json`；各 4 次模型请求、3 条 SQL。 |
| 仅选取阶段限制时间，之后累计全部关联记录，再排序取第一 | 实体 1，总数值 16 | `regroup-final-a.json`、`regroup-final-b.json`；各 4 次模型请求、3 条 SQL。 |

上述真实模型测试不访问实际业务数据库、不写生产元数据。每个成功记录都验证了结果和审计。模型表现存在波动，这些用例不等于对任意问题准确率或时延的保证；也没有把多次修改后的结果伪称为严格随机对照实验。

复现使用当前已配置模型，不覆盖其超时、重试和思考设置：

```powershell
.\.venv\Scripts\python.exe scripts/evaluation/prompt_regression.py --case existence --output artifacts/prompt-refactor/existence-check.json
.\.venv\Scripts\python.exe scripts/evaluation/prompt_regression.py --case regroup --output artifacts/prompt-refactor/regroup-check.json
```

## 部署与回退

仅更新 API 和语义规划组件，仍为原六个常驻服务。启动前检查近期无正在执行的用户任务；API、Planner 健康检查通过，API 容器内 54 个 Python 源文件与本地逐个哈希一致。

- API：`sha256:941e419813af46fe4b833388c889eadfae6c403e8857e97476721c29bed6d87f`
- Planner：`sha256:5334a0906bf86047c1ac2e4df8dd54170cfd7c85f113c56a8e7a50f94771fee4`
- 旧镜像：`semora-api:pre-prompts-20261107`、`semora-semantic-planner:pre-prompts-20261107`。
- 当前输出契约：[完整 Schema](D:/Develop/semora/docs/prompt-output-contracts.json)。
- 原提示词基线：[调用清单](D:/Develop/semora/docs/llm-call-inventory-20261107.md)。节点审查：[职责与输入输出](D:/Develop/semora/docs/prompt-node-responsibilities.md)。

回退时将两个旧标签重新指向 Compose 使用的 `semora-api:dev` 和 `semora-semantic-planner:latest`，再执行 `scripts/update-mvp.ps1 -Services api,semantic-planner`。数据库结构、业务域、模型配置和知识绑定均无需回退。
