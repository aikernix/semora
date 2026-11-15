# 实施状态

> 当前状态核对日期：2026-11-16。产品入口以 [README](../README.md) 为准，技术分工见[当前系统架构](architecture/agentic-architecture.md)。

## 当前实现

- 五类常驻服务：Web、API、Ontology、Executor、PostgreSQL；三个一次性 schema 迁移任务。模型与知识库使用外部集成，默认关闭演示种子。
- 数据源保存后进行版本、能力与结构采集；关联分析由用户显式触发。连接参数创建后不修改，支持重命名及新建连接。
- 单源及跨源关联发现支持关键词召回、分组整理、有界研究、工作记录、检查点与恢复；关系管理支持已生效、待审核、已停用及人工编辑。
- 业务域组合数据源包引用、跨源关系和自定义配置；查询绑定生效快照，模型分析与实际执行保持配置一致。
- 查询统一使用 LangGraph / SQLProgram / SQLGlot / Executor；支持必要澄清、SQL校验、有限修复、跨源完整键传递及查询级 DuckDB 合并。
- SQL生成、对照及修复只接收物理名称投影；紧凑上下文减少重复结构，原始本体、类型、枚举和条件保留。超过100条返回结果跳过模型结果说明。
- 数据源、本体、跨库关系与知识库绑定使用卡片、抽屉和弹窗交互；任务可取消，页面只在有活动任务时持续轮询数据源状态。

## 本轮验证记录

647项后端回归通过、6项显式跳过；23项受影响页面回归通过；Web、Ontology、Executor镜像构建及依赖策略检查通过。这些是本轮检查结果，不是未来提交的固定测试数量或完整生产认证。

单表、两表、三表口语查询已在本地页面执行，SQL分别访问对应数量的表，各返回20条，与独立只读查询逐行一致。三表结果说明曾将“前20条”称为“全部符合条件的数据”，文字问题仍未修正；本轮不能据此宣称结果说明全面准确。

## 历史实施记录

以下保留早期方案及验收背景，不作为当前功能清单、启动方式或部署承诺。

<details>
<summary>展开早期完整系统、历史组件与现场适配记录</summary>

> 2026-11-10 多数据库扩展：新增 JDBC 产品入口、驱动管理、统一结构快照、异步扫描和 MDL 导出。当前在线查询使用 SQLProgram / SQLGlot / Executor；下文历史 Semantic Planner 描述不代表当前部署。实际接入边界、验收与回退见 [MULTIDATABASE.md](MULTIDATABASE.md)。

# 实施状态

> 历史实施记录：以下描述对应早期完整系统，包含已移除的组件，不作为当前部署说明。当前服务与启动方式以仓库 README 和 `docs/sql-generation-mvp.md` 为准。

## 首版完整系统（已验收）

截至 2026-11-03，实施计划约定的 PostgreSQL 首版已经形成可独立部署、可配置业务域、可完成银行查数闭环的系统。以下能力不是页面原型，而是 Docker 演示环境中真实连接 PostgreSQL、对象存储、Keycloak、Wren Core 和各内部服务运行的实现。

- Vue 3 + TDesign 的 Semora 工作台、知识库、本体、数据源、审批、审计和设置页面。
- 查询结果同时提供表格、由已脱敏结果确定性生成的多指标图表和自然语言说明，图表不接触 Executor 原始敏感数据。
- 会话在创建时绑定业务域，前端可动态选择和创建业务域；查询运行时固定读取该域的已发布本体快照，不再将零售信贷写死在会话或工作流中。
- FastAPI `/api/v1` 契约、带游标的实时 SSE 事件流、三角色边界、结构化 QueryPlan。
- SQLGlot 只读/单语句/LIMIT/机构范围/字段权限/敏感数据门禁。
- 风险审批及 SQL 哈希变化后自动失效。
- 隔离 Executor、PostgreSQL 只读用户、只读事务、超时、行数与字节限制，以及结果离开 Executor 前的强制敏感字段脱敏。
- PostgreSQL 元数据 schema，以及会话、Run JSON 和审计事件持久化。
- Keycloak JWT/JWKS 严格校验；Docker 演示环境默认启用 Authorization Code + PKCE S256，API 使用外部 issuer 校验和容器内 JWKS 地址。
- Go Ontology Core：PostgreSQL 持久化草稿/候选/快照、完整投影校验、关系路径 BFS、不可变内容哈希，以及可由 Wren Core 直接加载的 Model、双向 Relationship、计算列和 Cube/Measure MDL 产物。
- Semantic Planner 服务、零售信贷样例库、Keycloak realm、SeaweedFS S3 兼容对象存储和 Compose。
- 内建 Knowledge Core：PDF/DOCX/PPTX/XLSX/HTML/Markdown/TXT/图片解析，图片、扫描 PDF、Word/PPT 内嵌图片的中英文 OCR，重叠分块、内容哈希、异步状态、pgvector 向量索引、pg_trgm 关键词索引和 RRF 混合检索。
- 文档解析已从进程内后台任务迁移为 PostgreSQL 持久化队列与独立 Knowledge Worker；支持 `SKIP LOCKED` 并发抢占、租约心跳、崩溃接管、指数退避、三次自动重试、人工重新解析和 Worker fencing。切块替换、文档启用和状态更新在同一事务中提交，检索只读取完整启用版本。
- 原文已接入 S3 兼容对象存储抽象：演示环境使用固定版本 SeaweedFS，生产可配置银行 S3；生产 Helm 默认要求 `aws:kms` 与 Bucket Versioning，写入携带 KMS/桶密钥参数并在读写时核验对象加密元数据，校验失败安全终止并清理未登记对象。历史 volume 原文由迁移容器搬迁，Worker 使用临时副本解析，外部详情不泄露对象 URI，下载经过 RBAC 并以 SHA-256 内容哈希验证。
- 文档上传、解析状态、知识库列表和检索证据已接入 Web 与查询工作流；Knowledge Core 不依赖外部 WeKnora 或 OCR 服务。
- Ontology Core 的对象、属性、关系、指标、草稿、候选审核、发布快照、内容哈希和关系寻路均为 Semora 内建实现，不调用外部 BKN 服务。
- 本体发布快照同时绑定数据源与知识库集合，并携带权限范围字段、时间字段、默认过滤、业务别名和可选关系；查询只能检索该快照绑定的知识库并只能路由到该快照绑定且已验证只读的数据源。
- 知识文档现可抽取为带文档/分块/修订证据的本体候选：未配置模型时按已发布映射做确定性抽取，配置模型时使用严格 JSON 和对象/属性白名单；模型不能直接发布或引入未知物理映射。
- 数据管理员接受结构化候选时，Ontology Core 在单一数据库事务中锁定候选与草稿、应用 patch、执行完整本体验证、自动递增补丁版本并记录审核人/意见；失败时草稿和候选状态同时回滚。定义类候选只记录治理结论，不擅自修改执行语义。
- Analyst 支持 OpenAI-compatible 的结构化模型输出，并以发布本体做白名单校验；未配置模型时使用确定性分析，模型越界输出不会进入 SQL 编译。
- Reviewer 已实现执行前与执行后两次独立复核：前者只接收不可变 QueryPlan、Gate 和 SQLArtifact，后者只接收 QueryPlan 与已脱敏的结果样本/摘要；Reviewer 无执行权限、无 Analyst 会话上下文且不能覆盖阻断门禁。可选结构化模型失败时按配置安全失败或回退确定性规则。
- Semantic Planner 固定使用 `wren-core-py==0.8.2`，直接消费本体发布的不可变 MDL，动态展开表、字段、关联和绑定参数，再由 SQLGlot 转成 PostgreSQL 方言；不再依赖固定零售信贷 SQL 模板，也不持有数据库凭据。
- 执行链路只向 Executor 传递数据源标识与密钥引用；连接配置由 API 转发到 Executor 加密持久化，兼容原环境变量，数据库凭据不进入 Agent 上下文。
- 本体属性的 `sensitive` 标记会进入 `SQLArtifact.sensitive_columns`，由 SQL Gate 触发审批，并由隔离 Executor 在结果离开服务前执行通用脱敏。
- 审计账本已改为 PostgreSQL 权威源：按 Run 使用事务级 advisory lock 原子分配连续序号，插入触发器校验前序哈希与不可变列，API 账号仅有 SELECT/INSERT 权限，UPDATE/DELETE/TRUNCATE 被撤销并由触发器二次阻断；哈希链回放和数据库篡改测试均已通过。
- Docker Compose 健康依赖和自动重启策略；已通过聚合查询、风险审批、敏感字段脱敏、真实 PostgreSQL 执行及审计链验证。
- 已验证 Knowledge Core 文档上传到解析完成、本体无效关系阻断、本体重启持久化、候选审核、两跳关系路径和完整查数回放。
- 数据源登记已持久化；页面支持新增、编辑连接参数，密码不回显且留空保留。Executor 负责加密保存凭据、真实连接测试、默认只读/危险角色能力校验，以及表、列、主键、外键和敏感等级扫描。WeKnora 地址与 API Key 可在知识库页配置并即时生效。
- 查询生命周期已迁移到真实 LangGraph 状态图，使用 PostgreSQL Checkpointer；已验证敏感查询在审批中断后重启 API，仍能从同一 `run_id/thread_id` 恢复并只执行一次。
- 系统设置已由静态页面升级为 PostgreSQL 持久化策略中心，保存审批模式、触发条件、查询限制、Reviewer 展示配置、版本哈希和发布人；查询接口不能覆盖管理员审批策略。
- 审批现已绑定 SQL/参数产物、本体快照、权限范围和策略版本；任一绑定项变化后旧审批自动过期。查询级限制可动态传入 Executor，但只能收紧容器硬上限。
- Executor 已提供受内部令牌保护的只读 PostgreSQL `EXPLAIN (FORMAT JSON)` 成本估算；估算结果进入门禁和审计，大结果集及 ABAC 标记的跨机构请求可按策略触发审批，估算不可用时安全失败。
- 查询授权不再由工作流硬编码：OIDC 的机构、角色、敏感级别和 ABAC 版本形成 Run 授权快照，机构 ID 进入 QueryPlan 与 SQL 绑定参数；空机构范围或越级敏感字段在执行前阻断。
- API 已按业务用户、分析员、数据管理员、审批员、审计员和管理员执行操作级 RBAC；前端导航按令牌角色显示，审批人身份取自令牌而不接受请求伪造。
- 会话持久化绑定 OIDC subject；会话列表、消息提交、Run 详情、SSE 和取消接口执行资源级所有权校验，同角色用户不能读取或操作他人的查询，管理员保留受控运维视角。
- 查询工作台支持会话搜索及最近 Run 回看；历史接口复用会话所有权校验，不通过前端隐藏代替服务端授权。
- 查询 POST 已改为后台 Run 并立即返回 `202`；SSE 增量推送执行中事件，支持断点游标。取消待审批任务会使审批失效，取消执行中任务会调用 psycopg `connection.cancel()` 中断指定 PostgreSQL 语句。
- Playwright 已覆盖真实 Keycloak PKCE 登录、知识文档上传、解析完成等待、本体候选抽取/审核、本体发布、聚合查询、实时结果、两阶段 Reviewer 结论、审计哈希验证、敏感查询审批和脱敏结果；测试使用系统 Chrome，失败时保留 trace 和截图。
- PostgreSQL 结构升级由一次性 `migrate` 容器在 API 启动前执行，应用账号不再尝试修改其不拥有的表。
- Knowledge Core 的结构初始化和演示种子由一次性 `knowledge-migrate` 容器执行，API 与 Worker 不并发执行 DDL；Nginx 通过 Docker DNS 动态解析 API，已验证 API 容器滚动重启时 Web 无需重启。
- Ontology Core 运行服务只校验既有 schema，不再持有或执行 DDL；本体升级和演示种子由独立 `ontology-migrate` 任务执行，生产迁移固定关闭演示种子。
- 元数据迁移已封装为非 root Migrator 镜像；DDL 账号与 API 运行账号分离，`semora_meta` 撤销运行账号的 schema CREATE 权限，审计表仍保持仅 SELECT/INSERT。
- 已提供生产 Helm Chart：外接银行 PostgreSQL、OIDC 与 S3，包含元数据、LangGraph Checkpointer、本体和知识四类有序迁移 Hook，以及八类可扩缩运行工作负载、非 root/只读根文件系统、健康检查、PDB、NetworkPolicy 和运行时 OIDC 配置；不打包演示基础设施和演示账号。
- API、Knowledge Core、Semantic Planner 和 Executor 已接入固定版本 OpenTelemetry SDK，通过 OTLP/HTTP 向 Collector 输出 Trace 并自动传播跨服务上下文；查询根 Span 关联 Run、Session、业务域与审计 trace ID。为避免旁路泄露，数据库自动插桩被显式禁用，SQL、参数和认证头不进入通用观测后端。Compose 提供固定版本演示 Collector，Helm 通过配置对接银行现有 Collector，不在业务命名空间内自建观测后端。
- 独立 Audit Exporter 已实现终态 Run 的异步合规封存：导出前重算数据库事件链，将完整审计包写入内容寻址 S3 Key，并用条件写阻止覆盖；生产 Helm 默认 `aws:kms`、Bucket Versioning、`Object Lock Compliance` 与 10 年保留，缺少 KMS、版本控制、对象加密元数据或 Object Lock 时安全失败。可选 Kafka 使用幂等生产者，只发布对象 URI、内容哈希和终态哈希，不复制 SQL/事件正文；数据库归档登记为追加式并由触发器阻止更新/删除。
- 审计中心提供按问题、Run ID、Trace ID 和状态检索的查询索引，展示主体、事件数与归档状态；回放页面同时展示数据库链校验、外部归档内容校验、WORM 保留期和 Kafka topic/partition/offset。审计集合、事件、校验和归档代理均执行 auditor/admin 角色及 Run 存在性校验，终态 Run 在数据库层禁止继续追加事件。
- 供应链门禁已加入 CI：Python 直接依赖强制精确版本，Node 强制 lock integrity，Go 模块与基础镜像强制版本化；逐服务执行 `pip-audit`、Web 执行 `npm audit`、Ontology Core 执行可达性 `govulncheck`，并生成 CycloneDX SBOM 与许可证报告。已升级 FastAPI/Starlette、PyJWT、python-multipart、pypdf、Go/pgx/x-text，清除当前五个 Python 运行依赖集、Web 运行依赖和 Ontology Core 的已知可达漏洞。

## 银行现场适配与首版后演进

以下项目不阻塞 PostgreSQL 首版交付；它们依赖目标银行基础设施、数据源类型或后续产品范围，不能用本机演示配置替代现场验收。

- 增加版面/表格 VLM 结构化解析，并在目标银行对象存储联调生命周期/跨区域复制策略；应用侧 KMS 写入、Bucket Versioning 强制和逐对象加密校验，本地 OCR、S3 后端、持久化任务队列与知识口径候选审核流已经完成纵向闭环。
- 增加 Oracle、SQL Server、Hive/Trino 方言适配，并将当前 OIDC 声明式 ABAC 适配到银行真实数据范围服务。
- 增加多地域归档复制与银行真实 Kafka/Object Lock 联调；对象存储导出、WORM/Kafka 归档实现、OpenTelemetry Trace/Collector 接口、Helm 部署骨架、迁移权限分离和安全上下文已经完成，仍需在目标银行 Kubernetes、镜像仓库、密钥系统、Kafka、WORM 对象存储和可观测平台上做环境验收。

## 当前演示环境

- Web：`http://localhost:5173`
- API：`http://localhost:8000`
- Keycloak：`http://localhost:8088`
- PostgreSQL：`localhost:15432`
- 默认核心服务与 SeaweedFS 对象存储已通过 `docker compose up -d --build` 启动；S3 API 暴露在 `localhost:19000`，管理端口为 `localhost:19001`。


</details>
