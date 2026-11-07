# Semora

Agent每次模型请求均执行无损上下文精简：仅删除JSON字符串外的格式空白，并将完全相同的工具返回替换为对本次请求中原始完整结果的引用。原始消息和审计记录保持完整，不使用生成式摘要删除或改写业务事实。`AGENT_CONTEXT_MAX_BYTES`默认256000，预算包含消息和工具Schema；这是UTF-8字节预算，不是精确token数。去重后仍超限会明确停止并提示拆分需求，不静默截断权限、用户口径或证据。`agent.context.prepared`记录压缩前后大小和复用次数。

当前分支支持跨实例分步查询：Analyst生成`source_steps`，Reviewer整体复核后，由程序先查询来源A、提取并去重关联键，再参数化查询来源B，可按唯一键补充A的输出列。各步独立连接、只读执行和审计；中间结果截断或键类型不兼容时停止，不拼接不完整结果。原有`stages`仍表示单条SQL内的预聚合，与跨源步骤不同。

默认业务歧义澄清后自动生成、复核并执行，不再要求额外点击“确认生成SQL”。管理员可通过`require_plan_confirmation`和`approval_mode`恢复人工确认/审批；当前演示模式为`false`/`disabled`。执行权限、敏感字段和SQL门禁不受此开关影响。

跨源演示可在部署后用`.venv/Scripts/python.exe scripts/init-cross-source-demo.py --source-id <已登记MySQL来源ID>`初始化。脚本只读来源库，在本地PostgreSQL独立schema创建合成演示关联表并发布`cross_source_demo`本体，金额不是业务真实余额。选择“跨源分步问数演示”业务域，输入：“先在来源库按客户编号统计记录数，再查演示关联账户的金额和币种，把记录数一起展示，不限制时间。”详见[跨源设计与验收](docs/cross-source-query.md)。

当前 MVP：新建会话先提交需求单，由配置的大模型理解需求并逐轮澄清；口径明确后生成、复核和执行 SQL。模型连接在“模型与设置”页配置，支持 OpenAI 兼容 API、连接测试和后端持久化；未配置模型时提示先配置，不执行规则替代的 AI 分析。

知识库为可选辅助：默认通过 WeKnora Wiki API 检索、读取关联页和回溯来源，由模型决定研究动作。未绑定、未命中或知识服务不可用时，继续基于需求、本体和真实字段分析。需求单、用户补充和确认口径保留在会话中。未发布本体可以用于需求澄清，正式执行仍需发布本体。

Agent 使用 LangChain `create_agent` 原生工具循环，LangGraph 管理持久化与用户确认。Analyst 默认最多16轮模型调用、12次工具调用；Reviewer各次预检/复核最多4轮模型调用、3次补查，并只接收相关字段与关系目录。Agent总预算900秒，单次模型读取超时300秒，读取超时默认重试1次。技术修订保留已确认语义时内部重编译和复核，语义变化重新确认，最多修订3轮。阶段式QueryPlan将预聚合、关联、最终汇总编译为一条SQL；粒度和关联放大由程序门禁校验，不能只用说明文字声称已修复。

“提交补充”接口立即返回 `202`，后端继续执行，页面通过 SSE 显示工具操作、校验结论和修订记录；内部 Agent 消息只保存在后端，不返回浏览器。补充命令先持久化，重启后由现有 PostgreSQL Checkpointer 恢复待处理任务。外部知识库失败可跳过；数据权限、SQL 门禁及执行审批不可由 Agent 绕过。

下方内建 Knowledge Core、审批和合规部署内容属于先前实现及可选能力；当前 MVP 默认采用以上流程。

Semora 的 Ontology Core 是本仓库内建能力，负责对象、属性、关系、指标、草稿和发布版本。当前知识能力默认调用外部 WeKnora Wiki API；本仓库先前实现的 Knowledge Core 保留在 `native-knowledge` Compose profile 中，可选启用。BKN 不作为运行服务依赖。

Semora 是通用自然语言查数平台，当前业务示例为银行查数。产品入口聚焦一个最小闭环：配置本体和知识来源、由大模型理解业务问题、与用户澄清业务口径，再生成并复核只读 SQL，通过后自动执行，最后展示证据、可核验分析步骤、SQL 和结果。

查询工作台采用五步交互：

1. 提出问题
2. 理解需求
3. 确认业务口径
4. 生成并审核 SQL
5. 查询结果

系统在第 3 步前不会生成 SQL。存在“客户数”等歧义时，会提供业务选项和原因；没有歧义时默认自动冻结需求版本并继续，管理员可开启额外的人工方案确认。明确后的需求带版本与内容哈希，SQL 与该版本绑定，修改口径会使旧 SQL 和审批失效。界面只展示检索证据、结构化分析步骤、假设和决策，不展示模型隐藏思维链。

当前实现范围和后续生产化工作见 [实施状态](docs/IMPLEMENTATION_STATUS.md)，Agent 权限与依赖边界见 [Agentic Architecture](docs/architecture/agentic-architecture.md)。

## 当前可运行能力

- Vue 3 + TDesign 查询工作台与管理页面
- 内建 Knowledge Core：多格式解析、中文/英文 OCR、分块、pgvector 向量索引、关键词索引、RRF 混合检索及带分块证据的本体候选抽取
- PostgreSQL 持久化知识导入队列与独立 Knowledge Worker：任务租约、心跳、崩溃接管、指数退避、有限重试、人工重新解析和原子索引发布
- S3 兼容原文存储：演示环境内置 SeaweedFS，生产可替换为银行对象存储；生产 Helm 默认强制 `aws:kms`、Bucket Versioning 和逐对象加密校验，数据库不暴露内部对象 URI，下载经过 Semora 身份与权限校验并校验内容哈希
- 多业务域会话：创建会话时绑定业务域，每次查询固定使用该域已发布的本体版本、知识库集合和数据源
- 会话搜索和历史 Run 回看；历史加载与实时查询使用相同的主体所有权隔离
- 动态本体工作室：可在页面创建业务域，维护对象、属性、关系、指标、别名、权限字段、时间字段、默认过滤、知识库和数据源映射并发布不可变快照
- 零售信贷演示本体、知识证据、数据源与查询样例
- 持久化数据源登记、Executor 密钥引用、只读账号验证和真实 Schema/主外键/敏感等级扫描
- 结构化 `QueryPlan`、语义 SQL、物理 SQL、门禁和两阶段 Reviewer 结论：执行前复核计划/SQL，执行后复核已脱敏结果
- 查询结果表格、由已脱敏结果确定性生成的多指标图表及自然语言说明
- LangGraph 七节点查询生命周期、PostgreSQL Checkpointer 和跨进程审批恢复
- 后台 Run 与带游标的实时 SSE 事件流；取消信号可下沉到 PostgreSQL `connection.cancel()`
- `disabled | all | risk_based` 审批策略
- PostgreSQL 持久化、带版本和内容哈希的系统策略；查询用户不能逐次关闭审批，策略变化会使旧审批失效
- OIDC/演示主体的机构范围与敏感级别形成不可变授权快照，并贯穿 Run、QueryPlan、SQL 参数、审批绑定和审计事件
- 会话和 Run 绑定 OIDC 主体，列表、详情、SSE 与取消操作执行资源级所有权隔离
- PostgreSQL 只读执行器，30 秒超时、1000 行和 10 MB 限制
- Executor 使用只读 `EXPLAIN (FORMAT JSON)` 提供真实行数/成本估算，大结果集由策略触发审批
- PostgreSQL 原子追加审计账本：按 Run 事务锁连续分配序号、校验前序哈希、数据库禁止 API 更新/删除，并支持哈希链回放校验
- 独立 Audit Exporter：终态 Run 自动封存为内容寻址 JSON 对象，归档前后重算事件链与内容哈希；生产默认要求 `aws:kms`、Bucket Versioning 和 S3 Object Lock Compliance，Kafka 仅投递归档指针和哈希，数据库归档登记同样禁止更新/删除
- 审计中心：审计员可按问题、Run/Trace ID 与状态发现记录，从索引回放原始事件，并核验数据库链和外部归档；非审计角色无法通过猜测 Run ID 读取审计数据
- 银行级供应链门禁：精确版本与锁文件校验、Python/Node/Go 漏洞扫描、许可证策略及逐服务 CycloneDX SBOM；WeKnora/OpenBKN 未审核源码目录会被 CI 阻断
- OpenTelemetry OTLP Trace：API、Knowledge Core、Semantic Planner 与 Executor 自动传播 W3C Trace Context；查询根 Span 绑定 `run_id`、业务域和审计 trace ID，明确不采集 SQL、绑定参数或认证头；Compose 内置仅用于演示的 Collector，Helm 对接银行现有可观测平台
- 独立 Ontology Core（Go）和 Semantic Planner（Python）服务边界
- Ontology Core 发布标准 Wren MDL Model、双向 Relationship、计算列和 Cube/Measure；投影失败会阻止发布，历史快照按投影器版本安全升级
- Semantic Planner 固定使用 `wren-core-py==0.8.2` 展开发布 MDL，再由 SQLGlot 转为 PostgreSQL 方言；Planner 容器不配置也不持有业务数据库凭据
- Semantic Planner 完全由已发布本体驱动表、字段、关联方向、可选关系、机构权限和时间条件，不包含零售信贷专用 SQL 模板
- 知识文档到本体的治理闭环：确定性/可选 LLM 抽取、结构化候选、安全白名单、数据管理员审核、事务式草稿合并、版本递增和发布
- Playwright 端到端验收：PKCE 登录、知识导入、候选抽取与审核、本体发布、查询、审批、结果 Reviewer、脱敏结果与审计回放

## 启动

本机最小 MVP（推荐，复用已构建镜像）：

```powershell
.\scripts\start-mvp.ps1
```

更新已构建的 API/Web 镜像使用 `.\scripts\update-mvp.ps1`（仅更新 API 时加 `-Services api`）。启动和更新脚本均先读取本项目 `.env`，避免父进程中其他项目的凭据覆盖。Compose 的 WeKnora 连接配置使用项目专属的 `SEMORA_WEKNORA_BASE_URL`、`SEMORA_WEKNORA_API_KEY`；容器内部仍映射为 `WEKNORA_BASE_URL`、`WEKNORA_API_KEY`。旧项目配置请将 `.env` 中这两个键加上 `SEMORA_` 前缀，值不变。

只保留 6 个常驻容器：Web、API、PostgreSQL、本体、语义规划、只读执行器。
LangGraph、Analyst 和 Reviewer 在 API 进程内运行。知识库使用已部署的外部 WeKnora，业务数据仍从配置的外部 MySQL 读取。
迁移容器执行一次后退出；登录、内建知识库、审计外部归档、对象存储和链路采集不启动。
查询审计事件仍写入 PostgreSQL。停止用 `.\scripts\stop-mvp.ps1`，保留数据。

下方命令启动完整环境，首次构建也可使用：

```bash
docker compose up --build
```

演示环境会同时启动 SeaweedFS S3 兼容对象存储；生产环境通过 `KNOWLEDGE_S3_*` 配置替换为银行现有对象存储，不修改 Knowledge Core 代码。Compose 为方便本机演示明确关闭 SSE/WORM；生产 Helm 默认要求 KMS 密钥、Bucket Versioning，并在启动和对象访问时安全校验。

启动后访问：

- Web: <http://localhost:5173>
- API/OpenAPI: <http://localhost:8000/docs>
- Ontology Core: <http://localhost:8081/healthz>
- Semantic Planner: <http://localhost:8001/healthz>
- Executor: <http://localhost:8002/healthz>
- S3 API: <http://localhost:19000>
- Object Store Admin: <http://localhost:19001>
- OpenTelemetry Collector health: <http://localhost:13133>
- Audit Exporter: <http://localhost:8004/healthz>

演示环境默认关闭登录，以便直接验证最小闭环。需要验证银行身份接入时，可将 `.env` 中的 `AUTH_REQUIRED` 改为 `true`，再使用 Keycloak PKCE 登录：

- 分析/审批/审计用户：`demo` / `demo1234`
- 系统管理员/数据管理员：`demo-admin` / `admin1234`

以上仅为本机演示凭据，生产部署必须删除种子用户并接入银行 IAM。

本地前端开发：

```bash
cd apps/web
npm install
npm run dev
```

本地后端测试：

```bash
python -m pip install -r services/api/requirements.txt
python -m pytest services/api/tests

# Wren Core 适配层
docker run --rm -v "${PWD}/services/semantic-planner/tests:/tests:ro" semora-semantic-planner \
  python -m unittest discover -s /tests -v

# Ontology Core
docker run --rm -v "${PWD}/services/ontology:/src" -w /src golang:1.23-alpine \
  go test ./...

# Knowledge Core OCR（在已构建镜像中执行）
docker run --rm -v "${PWD}/services/knowledge:/workspace" -w /workspace semora-knowledge \
  python -m unittest discover -s tests -v
```

完整浏览器验收（需要 Docker 环境已启动，并安装本机 Chrome）：

```bash
cd apps/web
npm run test:e2e
```

## Kubernetes / Helm

生产 Chart 位于 [`deploy/helm/semora`](deploy/helm/semora)。它只部署 Semora 自研组件，不在银行集群中创建演示 PostgreSQL、Keycloak、对象存储或可观测后端。安装前需由平台侧创建运行/迁移分离的数据库 Secret、内部调用令牌、Executor 数据源 Secret 和 S3 Secret，并配置银行 OTLP Collector；详细键名见 Chart 的 `values.yaml` 与 README。

```bash
helm lint deploy/helm/semora
helm upgrade --install semora deploy/helm/semora \
  --namespace semora --create-namespace -f bank-values.yaml
```

四个 pre-install/pre-upgrade Hook 会依次执行元数据、LangGraph Checkpointer、本体和知识迁移，且生产固定不导入演示种子。Web 的 OIDC 参数通过运行时 ConfigMap 注入，无需因 IAM 地址变化重新构建镜像。

## 安全边界

Agent/API 不持有业务数据库凭据。只有 Executor 接收由 API 签发的内部调用并连接业务库；即使上游生成了危险 SQL，Executor 仍会再次进行只读检查。审批与 SQL/参数、本体快照、权限范围和策略版本绑定。动态查询限制只能收紧、不能突破 Executor 的 30 秒、1000 行和 10 MB 硬上限。浏览器使用 Authorization Code + PKCE S256，API 同时校验 issuer、audience、签名和角色。演示环境使用开发凭据，生产必须接入银行 IAM 与密钥系统。

Reviewer 不持有执行权限，也不能覆盖确定性门禁。它以全新、隔离的上下文分别复核不可变的计划/SQL 产物和 Executor 返回的脱敏结果；未配置模型时使用确定性规则，配置 OpenAI-compatible 模型后可进行结构化 AI 复核。模型异常或结果结构不一致时安全失败，不生成最终自然语言回答。

