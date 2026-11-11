# 保守 SQL 能力基线

策略版本：`portable-read-v1`。这是 Semora 的生成与验证策略，不是“所有数据库版本已兼容”的认证。SQLGlot 和 JDBC 驱动版本保持原来的固定版本。

## 默认行为

源库查询优先使用普通 SELECT、INNER/LEFT JOIN、WHERE、GROUP BY/HAVING、派生表、CASE/COALESCE 和绑定参数。聚合、筛选、排序、关联键和输出粒度不能为了兼容而被删除或改变。具体函数差异仍由 SQLGlot 转换及数据库预检/实际只读执行验证；本策略不是任意 SQL 的完整语法白名单。

CTE、窗口函数、递归 CTE、SQL Server OFFSET/FETCH 使用三态记录：`true/verified` 为已验证，`false/unsupported` 为单项探测明确不支持，`null/unverified` 为未知。未知允许进入数据库预检和只读执行，不因缺少探测记录强制改写；明确不支持时进入既有定向纠正流程。普通 CTE 通过不能替代递归 CTE 验证，TOP 通过也不能替代 OFFSET/FETCH 验证。版本号仅作描述；权限不足、连接失败和超时都不视为不支持。

查询理解、SQLProgram、SQLGlot、Executor 的流程顺序和模型角色不变。模型上下文解释上述三态；不能保持原意时明确报告，不通过删除窗口、过滤、排序或偏移来强行交付。业务SQL的语法及功能错误按数据源记录到 `scan_result.sql_feedback`，只表示具体语句被拒，不能推断其中所有功能都不支持。最多保存32条去重记录，模型只接收最近4条；记录SQL哈希、函数名及已脱敏诊断，不保存SQL正文或参数值。后续修复立即使用该记录，单次任务最多5轮定向修复。

本地 DuckDB 步骤继续使用它自身的能力；跨源中间结果的完整性和行数限制保持原有规则。

## 分页

| 目标 | 默认物理 SQL 形状 | 限制 |
|---|---|---|
| PostgreSQL / MySQL | LIMIT | 可选偏移按实际探测记录 |
| Oracle / OceanBase Oracle 模式 | 排序放在内层，外层 ROWNUM 限制 | 默认不要求 FETCH；拒绝无法忠实转换的 OFFSET、WITH TIES、PERCENT |
| SQL Server / 显式配置的 TSQL | TOP | OFFSET/FETCH 未知时可预检，明确不支持时改写 |

驱动配置 `pagination=baseline` 是默认值。管理员可显式选择 `sqlglot` 或 Oracle 的 `rownum`；`scan_only` 保持关闭问数。显式选择不会绕过查询验证或自动证明目标版本支持该语法。查询编译和关系采样复用同一个分页转换函数。

Oracle 的排序子查询与外层 ROWNUM 依据 [Oracle 文档](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/ROWNUM-Pseudocolumn.html)；SQL Server TOP 与偏移分页分别参考 [Microsoft 文档](https://learn.microsoft.com/en-us/sql/t-sql/queries/top-transact-sql)。这些是语法依据，不替代历史版本实例验收。

## 探测、缓存与失效

用户点击“验证查询”时，原生 MySQL/PostgreSQL 和 JDBC 都执行有超时的只读合成探测，不采样业务数据。探测检查实际返回值，偏移探测验证跳过第一行，递归探测验证生成第二行。正常问数不重复探测，因此不增加每道问题的模型轮次或探测往返。

能力结果记录 `sql_baseline`。JDBC 验证签名绑定当前策略和有效分页配置；旧 JDBC 来源仍需通过基础查询验证。原生旧来源保留基本查询入口，未知高级能力可以在具体SQL上验证；未知不被标记为已支持。

重新测试发现产品、服务器版本、驱动版本或兼容模式变化时，清除旧的探测证据和SQL错误记录，并标记结构需重新扫描。相同目标的普通重扫保留仍有效的探测结果及SQL错误记录。

## 验证入口

- 策略和语义回归：`services/api/tests/test_sql_baseline.py`。
- 隔离实例：`deploy/compose.database-tests.yml`；MySQL 5.7 使用独立 `legacy` profile，数据均为合成数据。
- 历史版本的 Oracle、SQL Server 和国产库仍需实际实例验证，不因选择较低语法形状而自动登记为已支持。

```powershell
docker compose -f deploy/compose.database-tests.yml --profile mysql --profile legacy up -d
$env:SEMORA_TEST_DATABASES='postgresql,mysql'
$env:SEMORA_TEST_LEGACY='true'
.venv/Scripts/python.exe -m pytest
```

升级前备份及镜像位于 `artifacts/database-upgrade/*-sql-baseline/` 和 `semora-{api,executor,web}:before-low-sql-baseline`。

## 共享能力缓存（common-read-v1）

部署此版本需按顺序执行元数据库迁移至 `010-datasource-capability-storage.sql`。不新增常驻服务。保存的数据源连接测试通过后，后台自动验证；“验证查询”接口会等待正在进行的验证，`POST /api/v1/datasources/{id}/validate-query?force=true` 可强制全量重测。

- 首次探测 24 项：CTE、窗口、递归、分页、偏移、数值/空值类型，以及常用数学、字符串、聚合、类型转换和日期表达式。全部使用固定合成值，验证返回值，不读取业务数据。
- 相同产品、完整版本、兼容模式、方言、驱动版本、适配配置及探测目录版本共享基线。JDBC 驱动按实际 JAR 内容哈希区分，并纳入会话属性的指纹；仅同版本号不能保证命中。缺少版本或驱动身份时不共享。
- 新实例或新账号命中基线后，仍执行连接、只读和基础查询验证，再执行 4 项冒烟探测及未知或冲突项。相同连接、账号和配置的有效证据可直接复用，基础连接和查询验证仍执行。
- 共享和跨连接复用的证据默认有效 168 小时，可用 `SQL_CAPABILITY_CACHE_TTL_HOURS` 调整。复用不延长证据期限。版本、账号、连接选项或驱动变化后重新判断；不同来源结果冲突时记为未知。本源直接探测记录保留至环境变更或重测，本源明确不支持优先于共享支持；共享负面证据仅作提示，不直接禁止其他实例执行。
- 实际业务查询成功且返回行时，记录最终输出中的函数表达形式。记录函数、参数类别、表达式哈希及验证范围，不保存 SQL 正文或参数值；不推断所有重载、空结果或未执行的子表达式均被验证。每源及共享聚合最多保留 64 项此类观察，模型仅接收最近 16 项。
- 能力缓存只减少重复能力探测，每条业务 SQL 仍经过结构检查、目标数据库预检和只读执行。缓存不可用时重新探测，不把缓存故障作为业务查询失败原因；未知能力继续允许预检。

`GET /api/v1/datasources/{id}/capabilities` 返回能力证据及 `probe_cache`，其中 `executed`、`reused` 和 `mode` 区分本次执行与复用。单个合成表达式通过只证明该表达形式，并非数据库全部函数或所有版本兼容认证。


## 能力存储与数据源关联

| 存储 | 内容 |
|---|---|
| `semora_meta.database_capability_profiles` | 共享环境描述、能力基线和来源证据；profile_key 是环境描述的哈希。 |
| `semora_meta.datasources.capability_profile_id` | 指向共享档案的外键。多个实例、不同库名或账号可引用同一档案；账号和连接验证不共享。 |
| `semora_meta.datasource_capability_observations` | 每数据源一行：环境指纹、连接检测元数据、查询能力状态、单项探测、实际表达式观察与复用回执。能力项按函数/调用形式保存为 JSON 映射。 |
| `semora_meta.datasources.scan_result` | 表、字段、注释、键、扫描作业等结构快照；不再持久化 connection、capabilities、capability_cache、sql_observations。具体业务 SQL 的拒绝反馈仍为本源记录，不当作通用函数能力。 |

迁移 010 保留旧共享缓存并迁移本源探测、明确不支持记录和观察，随后移除旧表 sql_capability_baselines 及 scan_result 内的能力副本。迁移可重复执行，不覆盖已经迁出的较新本源记录；连接配置和密码仍归 Executor 所有。

为了避免扩大查询链路改动，Repository 在内存中组装兼容的运行视图：现有接口仍能返回 connection/capabilities，重启时从独立表加载，并从共享档案恢复临时复用视图。这些内容不会写回 scan_result。数据源保存及本源证据写入使用同一事务；关联档案随环境变化更新，清空连接元数据时取消关联。
