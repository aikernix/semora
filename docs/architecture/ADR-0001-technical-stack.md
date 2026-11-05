# ADR-0001：Semora 技术选型

- 状态：已选定
- 日期：2026-11-02
- 目标：面向银行的自然语言查数系统，展示分析依据、SQL、查询结果和自然语言说明，并提供独立复核、可配置审批和完整审计。

## 总体方案

Semora 采用 Python + Go 的模块化架构，部署时拆成 Web、API/Agent、Knowledge Core、Ontology Core 和 SQL Executor 五个安全边界。Agent 服务不持有业务数据库凭据；只有 Executor 可以连接业务库。Knowledge Core 与 Ontology Core 均由 Semora 自主实现、随仓库构建和随同一套部署交付；WeKnora、BKN Foundry 都不是外部运行依赖。

第一阶段参考 AskOda 已验证的需求版本、SQL 门禁、只读执行和审计回放思想，并参考 BKN Engine 的对象类、关系类、指标、资源映射等概念，在 Semora 中自主建立统一领域模型和 API。参考项目不作为运行时依赖，也不通过远程 API 调用。

## 技术栈

| 层 | 选择 | 说明 |
|---|---|---|
| 语言 | Python 3.12 + Go 1.23 | Python 负责 Agent 与业务编排；Go 承载 Semora 自主实现的本体模型、校验、发布和关系寻路核心 |
| API | FastAPI + Pydantic v2 | OpenAPI 契约、SSE 流式事件、异步工具调用 |
| 数据访问 | SQLAlchemy 2 async + Alembic | 管理 Semora 元数据；业务库查询走独立适配器 |
| Agent Runtime | LangGraph + LangChain 1.x | 持久化状态、失败恢复、人工中断和流式事件；不依赖 BKN Foundry 进程或 API |
| Agent 状态 | LangGraph Postgres Checkpointer | 不使用内存 checkpoint；`thread_id` 使用 UUID |
| 主数据库 | PostgreSQL 16 + pgvector | 保存会话、任务、本体、知识分块、向量、策略、审批和审计；不同 schema 隔离 |
| 知识服务 | Semora Knowledge Core + Knowledge Worker | 多格式解析、内建 Tesseract 中英文 OCR、持久化任务队列、租约恢复、原子切块发布、OpenAI 兼容 Embedding、pgvector、pg_trgm 和 RRF 混合检索；不依赖外部 WeKnora/OCR 服务 |
| 本体服务 | Semora Ontology Core（Go） | 内置对象类、关系类、值类型、指标、概念分组、资源映射、分支和发布快照能力 |
| 语义查询 | Semora 本体投影 + Wren Core 0.8.2 + 隔离 Semantic Planner | 本体是唯一事实源，发布时生成不可编辑的 Wren MDL Model、Relationship、计算列和 Cube/Measure；Planner 只做语义展开，不持有数据库凭据 |
| SQL 处理 | QueryPlan Builder + Wren Core + SQLGlot Gate | 固定 `wren-core-py==0.8.2` 将发布 MDL 与语义 SQL展开为物理 SQL，SQLGlot 转换目标方言并执行 AST、权限、只读、谓词和 LIMIT 门禁 |
| SQL 执行 | 独立 FastAPI Executor | 使用只读账号、事务只读、查询超时、行数/字节数限制、取消和结果摘要 |
| 前端 | Vue 3 + TypeScript 6 + Vite 7 | 使用 TDesign Vue Next，采用 WeKnora 风格但保留 Semora 独立品牌 |
| SQL/图表 | Monaco Editor + ECharts | SQL 查看与审阅；表格、趋势、分组结果可视化 |
| 身份认证 | OIDC；本地 Keycloak，生产对接银行 IAM | 前端不保存数据库凭据；JWT 只在 BFF/API 边界校验 |
| 授权 | Semora RBAC + ABAC 策略层 | 角色、数据源、表字段、机构范围和敏感等级共同决定权限 |
| 可观测性 | OpenTelemetry + OTLP Collector | Trace、Metric、Log 使用统一 `trace_id/run_id`；不依赖 SaaS |
| 业务审计 | PostgreSQL 追加写审计账本 + 哈希链 | 记录证据、模型/Prompt 版本、SQL、门禁、审批、执行和交付；可选导出 Kafka/WORM |
| 附件/导出 | S3 兼容对象存储，开发用 SeaweedFS | 保存知识原文、导出文件和大体积审计附件；生产替换为银行 S3/KMS，不绑定具体厂商 |
| 本地部署 | Docker Compose | 一键启动 Web、API、Knowledge、Worker、Ontology、Planner、Executor、PostgreSQL、Keycloak 和 S3 兼容对象存储 |
| 生产部署 | Kubernetes + Helm | API、Agent Worker、Executor 独立扩缩容；密钥由银行密钥系统注入 |
| Python 工程 | uv + Ruff + mypy + pytest | 锁定依赖并执行静态检查、单元和集成测试 |
| 前端工程 | pnpm + ESLint + Vitest + Playwright | 类型检查、组件测试和关键链路端到端测试 |

## Agent 与确定性控制边界

LangGraph 中只有三个岗位级 Agent：Conversation Owner、Analyst、Reviewer。知识检索、本体查询、Cypher/SQL 编译、SQL 检查、审批和执行均为工具或确定性服务。

- Conversation Owner 维护用户目标和任务状态，决定澄清、分析、复核或交付。
- Analyst 根据 Knowledge Core 证据和本体快照产生结构化 `QueryPlan`、候选 SQL 与说明。
- Reviewer 使用隔离上下文复核业务口径、关联路径、SQL 和执行结果，只能返回审查结论，不能执行查询或回复用户。
- Policy/Gate 是最终裁决者。Reviewer 通过不等于自动获得执行权限。

Agent 输出必须使用版本化结构，不将模型内部思维文本作为审计依据。对用户展示的是分析步骤、使用的证据、关键假设和审查结论。

## 知识、本体与 SQL 数据流

1. Knowledge API 保存原文并写入 PostgreSQL 持久化队列；Knowledge Worker 持租约解析和索引原始文档，以单事务发布完整切块，再由向量与关键词 RRF 混合检索返回带文档、分块、位置和内容哈希的业务口径证据。
2. Ontology Core 返回指定发布版本的对象、关系、指标、粒度、字段映射和允许的关联路径。
3. Analyst 生成结构化 `QueryPlan`，至少包含指标、维度、过滤、时间范围、粒度、关联路径和证据引用。
4. Ontology Publisher 将发布快照投影为标准 Wren MDL；隔离 Semantic Planner 内嵌固定版本 Wren Core Python Binding，根据 `QueryPlan` 与该不可变产物展开物理表，再经 SQLGlot 输出具体方言 SQL。运行链路不调用外部 Wren 服务，Planner 无数据库凭据；LLM 生成的物理 SQL不能直接执行。
5. Gate 执行 AST、权限、只读、成本预估和需求覆盖检查。
6. Approval Policy 根据配置决定直接执行、等待人工审批或拒绝。
7. Executor 使用受限账号执行，通过流式事件返回状态，并将结果摘要交给 Reviewer 复核。

## 审批配置

审批是策略配置，不是 Agent。全局默认使用 `risk_based`，并允许数据源和角色覆盖：

```yaml
approval:
  mode: risk_based  # disabled | all | risk_based
  require_for:
    sensitive_levels: [restricted, confidential]
    result_detail: true
    estimated_rows_over: 100000
    estimated_cost_over: 1000000
```

策略快照与生成的 SQL 版本绑定。SQL、参数、数据权限或本体版本发生变化后，原审批自动失效。

## 首版数据源

首版完整支持 PostgreSQL，并从第一天保留方言和连接适配器接口。MySQL、Oracle、SQL Server、Hive/Trino 作为后续适配器加入，不在核心逻辑中写 PostgreSQL 专属假设。

## 本体能力自主实现边界

Semora 不部署外部 BKN，也不调用 BKN API。下列能力以 Semora 自有数据结构、接口和测试自主实现：

- 参考 BKN Engine 的本体元模型思想，实现对象类、关系类、值类型、指标、概念分组、资源映射、草稿、候选和不可变发布快照。
- 首版不搬入 openCypher 编译器；保留本体查询接口，后续可增加只读 openCypher 查询能力。
- 参考 VEGA 的 Resource/Catalog 抽象实现 Semora 数据源适配层；首版只实现关系数据库所需的最小集合，不搬入完整 VEGA 平台。
- 参考 Context Loader 的按需加载方式，在 Python Agent Tool 层提供 schema 搜索、对象读取、关系路径和指标定义工具。
- BKN Safe 与 BKN Trace 不直接搬入；Semora 自行实现 RBAC/ABAC、审批、审计账本和 OpenTelemetry 链路。

`services/ontology` 是 Semora monorepo 内的源码模块。部署时允许它作为独立容器运行，API 服务通过 Docker 内网访问该容器；这是内部服务边界，不是对 OpenBKN、BKN Foundry 或其他第三方本体平台的远程调用。生产部署不得配置指向第三方 BKN 实例的地址。

当前实现未复制 OpenBKN 源文件。若未来确需引入上游源码，必须逐文件执行许可证检查；未经评估的 OpenBKN License 文件不得纳入。仓库持续保存 `THIRD_PARTY_NOTICES` 和来源记录。

## 明确不采用

- 不使用 AgentScope：首版选择 LangGraph 承载 checkpoint、人工中断和恢复机制。
- 不把完整 BKN Foundry 作为外部运行前置，也不把整个混合许可证仓库直接复制进 Semora。
- 不把 Wren 作为第二套业务事实源：Wren 产物只能由本体发布生成，禁止人工平行维护；实际 SQL 必须由 Semora 展示、校验和审计。
- 不使用 Java 重写首版：Python 已覆盖编排、SQL 和数据连接需求；Executor 的 HTTP 契约允许银行未来按要求用 Java 替换。
- 不使用 LangSmith 等外部 SaaS 保存银行查询内容。

## 版本与升级规则

- 依赖使用 lockfile 固定，禁止生产环境使用 `latest` 镜像。
- LangGraph/LangChain 锁定兼容版本；升级时运行 checkpoint 恢复和工具调用契约测试。
- Wren Core Python Binding 固定为 `0.8.2`；升级必须提升本体投影器版本，并通过 MDL 加载、Cube 编译、方言转换、门禁和真实只读执行回归。
- Ontology Core 是 Semora 自有模块；其模型、发布哈希、路径查询和投影契约必须通过回归测试后才能升级。
- 外部接口统一以 `/api/v1` 发布；数据库 schema、Prompt、Policy、Ontology 和 QueryPlan 均有独立版本号。
- Knowledge Core、Ontology Core 和数据源驱动只通过版本化内部接口进入核心域；组件升级不得破坏任务与审计契约。

## 验收基线

- 相同问题、本体版本和知识证据可以回放到相同的 QueryPlan 与门禁结论。
- Agent 服务无法直接连接业务数据库，Reviewer 无权调用执行接口。
- 任意查询都能关联用户、知识引用、本体版本、Prompt/模型版本、SQL、门禁、审批、执行摘要和最终说明。
- 写 SQL、多语句、未授权字段、缺少机构范围、超时或超限查询必须在执行前或执行中确定性终止。
- Knowledge Core 不可用或检索不到证据时安全停止 SQL 生成；Ontology Core 或编译器失败时禁止生成和执行无本体约束的 SQL。
