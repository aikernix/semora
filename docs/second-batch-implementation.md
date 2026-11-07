# 第二批：候选、任务契约、角色边界与 QueryCore

日期：2026-11-06。复用程序 Owner、Analyst、Reviewer 和现有六个MVP服务；没有新增Agent、框架或常驻组件。保留工作区原有修改，不提交Git、不修改外部业务数据。

## 实际改动

| 责任 | 实现 |
| --- | --- |
| 候选注册与失效 | semora_agent/candidates.py 的 CandidateRegistry；注册、规范内容校验、深拷贝、阶段清理、无效候选及重复失败计数集中管理 |
| 候选绑定 | task_contracts.py 的不可变 ArtifactBinding；覆盖运行、会话、业务域、阶段、原请求、本体、范围、权限、已确认需求及完整确认选项 |
| 分派任务契约 | TaskContract、TaskBindings、TaskLease；分派前验证契约，保留原上下文、完整用户口径、预算与迟到结果保护 |
| 角色工具 | app/agent_tools.py；不运行模型循环，不持有最终会话回复权；知识提供者通过构造注入 |
| 业务分析 | app/intent_analysis.py；只处理QueryIntent与业务修订，通过 AnalysisRolePorts 调用工具和角色运行器 |
| 独立复核 | app/review_analysis.py；预检、执行前与结果复核，通过 ReviewRolePorts 注入运行器，不导入兼容入口 |
| 旧协议 | app/legacy_plan_analysis.py；只在明确关闭AGENT_QUERY_INTENT时使用旧模型填写QueryPlan协议，独立回归 |
| 兼容入口 | app/agentic.py约50行，只组合/转发及保留旧导出；实现模块不反向导入入口 |
| 独立规划器 | packages/query-core/python/semora_query_core；业务意图解析、源拆分/键契约、阶段语义校验、SQL编译不依赖应用或Agent框架 |
| 持久复核状态 | QueryRun.review_case；保存规范问题ID、修订轮次、resolutions、计划/权限/输入绑定；不再写入模型工作区approved_preflight |
| 执行控制 | QueryRun.execution_binding、execution_receipts；只读执行进度与结果哈希独立于模型工作区，恢复必须同时匹配绑定、回执和原始事件 |
| 序列化边界 | app/control_state.py；API输出排除私有控制状态，元数据库JSONB显式持久化；兼容旧工作区格式，不需要建表迁移 |

## 修复了两个候选问题

新增回归先复现两项失败，再修复：

1. 已校验候选未绑定用户确认答案，确认口径改变后仍可引用；现在答案与选项完整说明都纳入绑定。
2. 候选字典对外暴露可变规范内容，修改快照可删掉筛选；现在只返回深拷贝，规范内容及哈希由Registry管理。

证据：`artifacts/source-planning/candidate-before.xml`、`registry-after.xml`及最终完整回归。

## 状态边界

- ConfirmedFacts：继续以用户确认答案和原始选项说明为依据，不由模型摘要推断；完整说明的兼容缓存保持原格式。
- IntentRevision：复用现有QueryIntent和不可变RequirementSpec版本/哈希，语义修改重新确认并废止旧执行权限。
- ArtifactBinding：候选采用类型化分量绑定；SQL/审批仍由现有实际SQL、参数、权限、本体及策略绑定规则控制。
- ReviewCase：已从松散agent_memory中迁出，并支持JSONB保存、问题ID及resolution往返。
- ExecutionReceipt：已从隐式进度字典迁出；回执被篡改时不能恢复后续查询。
- AgentWorkspace：保留角色私有对话、进度与知识/上下文缓存；不能凭其中的模型文字放行执行。旧控制键只在兼容读入时转换并移除。

TaskLease是**单进程**迟到结果保护，不是分布式租约。多实例CAS/事务版本列仍未实现，不宣称支持多实例并发修改同一任务。

## 联调发现与修复

真实模型在合成场景中合理地选择目标对象作为业务主体，同时对另一源的对象筛选。旧编译器要求先指定筛选端为主体，造成无效的能力缺口。现在对单一目标源、单一外部筛选源且无前置TopN的可证明场景，程序先取完整键集合，再参数化查目标源；不要求Analyst指派执行次序。

无法证明的多分支跨源聚合与选取范围仍明确停止。能力/配置缺口由程序形成终止交付，不再让模型继续调用已关闭的工具而产生“工具调用格式无效”。

真实模型联调使用固定SQLite people/accounts合成数据与已配置模型服务，**没有访问外部业务数据库，也没有写生产元数据**。修复前失败、零业务查询；修复后约30秒完成，4次模型调用（1次Analyst、3次独立Reviewer）、2次只读取数，结果为预期的A客户两条账户明细。证据：second-batch-model-before.json、second-batch-model-after.json。

## 验证与部署

具体测试数、Docker镜像与部署后的检查结果见本地 `artifacts/source-planning/second-batch-result.json`。

最终回归416项：415通过、1项因未配置独立元数据测试数据库跳过；最终镜像的真实Wren测试10项通过。部署后六个HTTP编译/合成结果用例全部通过，SQL哈希与更新前一致；临时JSONB持久化往返、两条历史查询结果及审计回放均通过。API和规划器已更新，六个MVP服务运行正常。依赖许可证/版本检查通过。

关键检查包括：默认QueryIntent成功和完整业务修订闭环、显式兼容协议、跨源复合键、TopN续聊、完整选项说明、权限/审批恢复、审计、候选失效、回执篡改、JSON持久化/旧数据兼容，以及QueryCore禁止加载应用、Agent和网络客户端的独立进程编译。

全局测试不再统一关闭QueryIntent；仅明确列出的旧协议套件设置兼容开关，新套件使用生产默认。真实模型验证与模型桩回归分别记录，不把整个测试数量描述为真实模型测试。

部署只更新API和semantic-planner。更新前镜像分别保留 `semora-api:pre-second-batch-20261106` 和 `semora-semantic-planner:pre-second-batch-20261106` 回退标签；本批没有数据库结构迁移。
