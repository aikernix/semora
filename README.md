# Semora

通用自然语言查数 MVP：需求工单 → 相关知识和本体 → 必要澄清 → SQL → 自动执行 → 查询结果与说明。业务内容由业务域、本体和知识库配置提供，核心代码不绑定行业。

## 本地启动

需要 Docker Desktop。配置 `.env` 后执行：

```powershell
./scripts/start-mvp.ps1
```

或：

```powershell
docker compose -f docker-compose.yml -f deploy/compose.mvp.yml up -d --build --remove-orphans web
```

页面：<http://127.0.0.1:5173>。设置页配置模型；数据源页配置只读连接并扫描结构；本体页发布映射及关系；知识库可绑定外部 WeKnora，也可暂不绑定。

常驻服务只有 Web、API、Ontology、Executor、PostgreSQL。`migrate`、`langgraph-migrate`、`ontology-migrate` 是一次性元数据迁移任务。业务数据库由 Executor 连接；API 仅在配置时转发凭据，不将数据库密码写入元数据或模型上下文。内嵌 DuckDB 只处理本次查询的中间结果，不是另一个常驻服务。

知识库页的“连接配置”支持修改 WeKnora 地址和 API Key，保存后立即生效；更换实例后需核对业务域的知识库绑定。数据源页支持通过地址、端口、数据库名、用户名及密码新增和编辑连接，密码留空保留原值。旧 `.env` 配置仍可使用，页面保存的连接优先。Executor 的加密连接文件和加密密钥持久化在 `executor-config` 数据卷中，备份与迁移时需一起保留；WeKnora 配置保存在平台元数据库中。升级时按顺序执行元数据迁移至 `010-datasource-capability-storage.sql`；数据源能力档案及本源验证记录已独立存储，并关联数据源。

本地知识库 API、解析 Worker、对象存储、内置 Keycloak 和 OTEL Collector 已从部署中移除。知识能力统一通过外部 WeKnora API 提供；现有认证适配器可按需连接外部身份服务。启动命令会移除同一 Compose 项目的旧孤立容器，不删除数据卷；已有数据库和历史任务保留。

## 代码入口

| 目录/模块 | 职责 |
|---|---|
| `apps/web` | Vue 3 / TDesign 工单、配置和结果页面 |
| `services/api/app/main.py` | HTTP、SSE、配置与会话入口 |
| `services/api/app/workflow.py` | 查询生命周期、取消及恢复 |
| `services/api/app/requirement_alignment.py` | 澄清卡片、确认事实、持久化中断/恢复 |
| `services/api/app/prepared_context.py` | 按需求检索知识、裁剪本体及确定性关系展开 |
| `services/api/app/research_tools.py` | 理解角色使用的受限资料工具，向 SQL 角色传递资料 |
| `services/api/app/sql_authoring.py` | 理解、SQL 生成、基于具体错误的有限修复 |
| `services/api/app/sql_dialect.py` | SQLGlot 解析、方言转换、参数和跨源键绑定 |
| `services/api/app/sql_execution.py` | 分步校验和执行，保持前置实际结果 |
| `services/api/app/result_engine.py` | 单次查询内存 DuckDB 计算 |
| `services/api/app/executor_client.py` | API 到隔离执行器的网络边界 |
| `services/agent/semora_agent` | LangGraph、工具调用运行时、模型传输和提示词 |
| `services/ontology` | Go 本体配置校验、版本发布及关系寻路 |
| `services/executor` | 业务数据库只读执行及结构化错误 |
| `packages/contracts/python/semora_contracts` | 持久化及 API 公共契约 |

只有 SQL 原生执行链路。旧 QueryIntent 运算编译器、候选规划协议、Reviewer、审批、审计导出和独立语义规划服务已移除；历史记录只做数据读取兼容，不恢复旧执行路径。非测试查询历史与业务配置保留。升级已有环境前先备份元数据库；`007-retire-audit.sql` 会清除已退休的审计表，不清理外部业务数据库。旧 Helm 原型已移除，目前维护 Docker Compose 部署。

具体流程、边界和维护规则见 [SQL 查询链路](docs/sql-generation-mvp.md)。

本体发布直接保存规范化定义及内容哈希，不再生成 Wren MDL 或要求通过 Wren 投影。旧快照及数据库的历史产物列保留读取兼容，不重算、不覆盖；查询只消费本体定义。关系中的固定值和复合条件原样保留，去重对象的声明列检查独立于任何语义引擎。

## 验证

安装 `services/api/requirements.txt` 和 pytest 后：

```powershell
python -m pytest services/api/tests
python scripts/verify_dependency_policy.py
cd apps/web
npm ci
npm run build
```

API 测试使用隔离元数据和 SQLite/DuckDB，不写业务数据库；覆盖实际关系运算、跨源传键、修复预算、澄清恢复、模型传输和数据源适配器。

多数据库配置、JDBC 扩展与 Wren MDL 导出见 [多数据库接入文档](docs/MULTIDATABASE.md)。产品入口和实例验收分别记录，不承诺未验证的版本或兼容模式。
