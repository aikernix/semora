# 2026-11-06：正确性修复与渐进职责提取

## 第一批 P1 产物（已验证并部署）

| 问题 | 修复前真实 SQL 结果 | 修复后结果 | 实现文件 |
| --- | --- | --- | --- |
| 必须匹配关系被预聚合改成 LEFT | total=300, quantity=7 | total=100, quantity=7；optional=true 时仍为300,7 | services/api/app/intent_planner.py |
| COUNT 经左关联后无匹配变 NULL | 第二主体 COUNT=NULL；全局空集 COUNT=NULL | 第二主体 COUNT=0；全局空集 COUNT=0；SUM/AVG仍保留NULL | services/api/app/query_stages.py |
| /query_intent/filters 被复核白名单拒绝 | 单元校验异常；默认生产协议修订闭环失败 | findings→业务意图修订→逐项resolutions→复核通过→执行和审计通过 | services/api/app/review_loop.py |

证据：`artifacts/source-planning/p1-before.xml`（初始16项中9失败、7通过），`p1-after.xml`（扩充后23项通过）。新增测试是从 QueryIntent 编译、结构校验、编译成 PostgreSQL SQL，再由 SQLGlot 转为 SQLite 并真实执行；不是外部生产数据，也不是新增真实 Wren 数据执行验收。

覆盖：正反关系方向、必须/可选匹配、多端筛选、全局空集、分组无匹配、有匹配、原字段NULL，以及SUM/AVG不强制转零。拒绝描述性字段、未知路径及负数组下标的修订路径。默认协议闭环测试删除 AGENT_QUERY_INTENT 环境覆盖，使用生产默认值；模型返回使用可控桩，实际执行只读SQLite查询，未绕过 Reviewer validator。

## 第二批：每项独立验证、可回退

1. **复核政策提取**：将 ReviewIssue、目标作用域、路径校验、问题台账、修订核验、修复路由移入只依赖契约和标准库的模块。API 的 review_loop 保留兼容导出；不把错误路由留在 SQL 编译器。
2. **解除应用模块循环**：intent_analysis 不再反向导入 agentic；由入口通过类型化端口注入工具会话、交付契约和独立 Reviewer 调用。现有默认与兼容路径分别回归。
3. **后续：候选与任务契约**：提取 CandidateRegistry 和 ArtifactBinding；将候选输入哈希、阶段失效、权限变动校验集中起来，再迁出 AgentTools 的候选状态。
4. **后续：角色调用边界**：逐步独立 Analyst、Reviewer 工具与提示词，agentic 退为兼容入口，Workflow 只协调持久生命周期。LangChain 继续只负责角色内部循环。
5. **后续：QueryCore 分层**：按实际业务对象提取语义解析、粒度/关联/空值逻辑计划、源拆步执行计划和方言产物边界；每步有 SQL 结果对照，拒绝为了目录创建空模型。编译代码不获取 Workflow、HTTP、数据库连接或模型对象。

## 状态边界：已实现与后续项

- ConfirmedFacts：现存用户答案和完整选项说明；不允许压缩成模型猜测。
- IntentRevision：现存 QueryIntent、需求版本与哈希；后续独立类型化版本状态。
- ArtifactBinding：现存需求/权限/本体/策略哈希与审批绑定；后续从散字典提取。
- ReviewCase：本批首先类型化 ReviewIssue 与作用域；持久问题台账的统一实体随后迁移，不冒充已完成。
- ExecutionReceipt：现存执行完成事件、行数/截断/哈希；后续提取显式回执结构。
- AgentWorkspace：现存角色私有记忆；不得作为执行、审批或用户确认事实的权威。

当前任务 guard 只保证单进程内迟到结果保护。**多实例 CAS/事务版本列尚未实现**，须后续单独迁移和并发验收。保持现有两个模型角色和程序 Owner，不增加常驻服务，不改权限、审批策略或超时预算。

部署沿用最小服务更新；记录旧镜像SHA供回退。保留工作区已有未提交修改，不进行外部业务库写入。

## 第二批本次实际完成

| 项目 | 状态与文件 |
| --- | --- |
| 类型化 ReviewIssue 与业务/产物作用域 | 已完成：services/agent/semora_agent/review_policy.py；不可变问题对象，禁止同一修订混合两种作用域 |
| 路径、台账、resolution、修订差异核验及路由 | 已提取至同一纯逻辑模块；services/api/app/review_loop.py 仅兼容导出，无 Workflow、HTTP、数据库、模型依赖 |
| 解除 intent_analysis ↔ agentic 导入循环 | 已完成：role_ports.py 注入工具会话、交付契约、Reviewer；intent_analysis 不再导入 agentic，AST 回归校验依赖方向 |
| 无效修订控制 | 已完成：Reviewer 指定的业务字段未变时，在第二次复核前停止；不允许用 resolutions 声称解决未修订问题 |
| CandidateRegistry、完整角色模块提取与持久 ReviewCase | 此历史批次未完成；随后已在[第二批实施](second-batch-implementation.md)中落地，详见该记录 |

### 最终回归和部署证据

- `p1-full.xml`：396项，395通过、1跳过；在首批修复后采集。
- `p2-full.xml`：399项，398通过、1跳过；涵盖默认 QueryIntent 业务修订闭环、兼容路径及新增边界。跳过项是缺少独立元数据数据库配置的审计数据库测试。
- `p1-wren.xml`：Linux真实 Wren Core编译测试10项通过；新增用例将物理SQL转SQLite执行并核验必须匹配、零计数、全局空集和nullable原字段。此处仍不等同于真实PostgreSQL/MySQL数据库执行验收。
- `p1-deployed-smoke.json`：向已部署 semantic-planner HTTP 提交6个合成计划，使用真实Wren编译，物理SQL在本地SQLite对照全部通过；没有访问业务数据库。
- 旧TopN续聊10→5的两条历史执行产物和审计在重启后读回验证通过；此次没有重新查询外部业务库。
- Docker仅更新api、semantic-planner；仍保持六个MVP常驻服务。没有调整权限、审批配置或模型超时。

镜像：

| 服务 | 更新前 | 更新后 |
| --- | --- | --- |
| API | 21211ce8bd878415cae63ae78e16d20bd9d8f1b2b5ed66d774d69ee49888229b | ab4bc59289560d2577aa322716a8debc6146eb5feac7f5d9add4af3e322c29eb |
| Semantic Planner | 39801a2a1c65549e1c2ff200823016acdbc7b533667b41deb19c2d3820a03fe8 | d6c87d3e55f94a50cd46666ed0cce861d6a8da7833ff976e9e6cab57e8a4dc24 |

可回退标签为 `semora-api:pre-correctness-20261106` 和 `semora-semantic-planner:pre-correctness-20261106`。需要回退时，先将对应旧标签重新标记为 `semora-api:dev` / `semora-semantic-planner:latest`，再运行现有 update-mvp.ps1，仅更新这两个服务；本批没有数据库结构迁移。

### 本批未覆盖/未扩大能力的边界

- 模型接口使用可控工具调用桩做修订闭环验收；本批未新增真实模型服务联调，不将398项旧新混合测试表述为398项真实模型验证。
- 没有新增原生PostgreSQL/MySQL合成表的写入验收；SQL结果对照基于SQLite，真实Wren负责语义编译。
- 任意多层预聚合、跨源分支联邦汇总、局部COUNT DISTINCT合并等仍受现有可证明性限制，不借本批放宽。
- 此历史批次的AgentTools/CandidateRegistry及控制状态边界已由[第二批实施](second-batch-implementation.md)更新；多实例CAS仍未实现。
- 工作区原有未提交修改保留。未提交Git，未改写历史审计，未向外部生产写入业务数据。
