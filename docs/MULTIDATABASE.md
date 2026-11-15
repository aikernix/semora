# 多数据库接入与 MDL 导出

本次实现保持 SQLProgram → SQLGlot → Executor 的查询链路。Wren Core 0.8.2 只校验导出的 MDL，不参与在线规划，不恢复 Reviewer、QueryIntent 编译器或审批服务。

## 产品、版本与实际能力

产品入口包括 PostgreSQL、MySQL、Oracle、SQL Server、OceanBase、GoldenDB、海量数据库 / Vastbase、自定义 JDBC。没有版本白名单，页面不要求手选版本。连接时保留数据库自动报告的产品、版本、驱动版本；`product_override` 是人工说明，不覆盖自动值，也不改变方言。

产品入口不等于已经通过实例验收。PostgreSQL 使用 PostgreSQL 原生协议，MySQL 与 GoldenDB 使用 MySQL 原生协议；Oracle、SQL Server、OceanBase、Vastbase 随 Executor 镜像分发固定版本 JDBC 驱动，其他厂商驱动及依赖可由管理员配置。OceanBase 需区分兼容模式，实际方言与查询能力仍由实例探测确认。

内置驱动：Oracle `ojdbc11:23.6.0.24.10`、Microsoft `mssql-jdbc:12.8.1.jre11`、OceanBase `oceanbase-client:2.4.13`、Vastbase `vastbase-jdbc:2.13.2v`。固定驱动版本不等于所有服务器版本已验证；其他厂商驱动可作为独立包上传。

连接、结构、注释、约束、MDL、查询分别记录状态。查询验证使用合成的 `SELECT 1`，并单独探测分页、CTE、窗口函数和基本结果类型；不能据此断言复杂业务 SQL 全部可用。Oracle 老版本不支持 FETCH、驱动不支持只读边界等情况会保留连接和扫描能力，报告实际失败，不绕过验证。

## 数据源操作

1. 在原数据源页面新增产品，填写账号密码及扫描范围。Oracle 可选 Service Name、SID 或无凭据的高级 JDBC URL；SQL Server 默认启用证书校验和加密，TLS 参数通过连接属性 JSON 配置。
2. 测试连接，检查自动识别值、Schema/Catalog；保存后异步采集版本和结构，不自动启动关联分析；连接参数创建后不可修改，需要变更时新增数据源。扫描范围接受逗号分隔的表标识。JDBC 表标识包括驱动返回的 Catalog、Schema、表名；PostgreSQL 为 `schema.table`，MySQL 为 `database.table`。
3. 扫描完成后检查权限缺口与声明约束，再显式点击更新关联关系。建模包含结构证据、模型召回与语义分析，按需读取统计证据；明确关系进入生效包，需人工处理的提议进入待审核。旧扫描候选的显式“采样验证”接口仍有独立预算，每侧最多200行、每查询3秒、1MB，不能将其等同于完整建模取证。样本唯一及样本缺失都不是全表结论。
4. 点击“验证查询”后，JDBC 来源才可以进入问数。自定义驱动必须先由管理员配置兼容方言和只读策略；配置未知时只扫描、导出。
5. 导出 ZIP：`mdl.json`（能够忠实构建时）和独立 `report.json`。存在未知类型、权限缺口、缺失关系端点等问题时只交付报告，扫描和现有查询不受导出失败影响。

## 驱动与高级适配

`/settings/database-drivers` 的写操作沿用管理员角色。普通用户不能加载停用包。一个包包含主 JAR 和依赖 JAR，限制 20 个文件、合计 100MB。保存 SHA-256、厂商版本、驱动类及启停状态。驱动类可填写，也可从 `META-INF/services/java.sql.Driver` 自动发现唯一实现。更换二进制包创建新 ID，不原地覆盖。

驱动配置示例（按实际产品填写，不能仅凭兼容宣传照抄）：

```json
{
  "name": "厂商 JDBC",
  "driver_class": "vendor.jdbc.Driver",
  "version": "实际驱动版本",
  "enabled": false,
  "dialect": "",
  "readonly_strategy": "",
  "pagination": "sqlglot",
  "type_mapping": {},
  "metadata_queries": {}
}
```

`dialect` 可选 `postgres/mysql/oracle/tsql`。`readonly_strategy` 可选 `postgres/mysql/oracle/sqlserver_permissions`；前三者执行数据库只读事务命令，最后者检查 SQL Server 当前用户在数据库、服务器和可见对象上的危险权限。不能确认策略时留空。`Connection.setReadOnly` 仅作为提示，不作为安全证明。`pagination=scan_only` 强制关闭问数；Oracle 老版本驱动包可配置 `pagination=rownum`，使用排序后的子查询加 ROWNUM 限制，仍需实际查询验证。不支持该策略下的 OFFSET，不会改写为含义不确定的分页。

`type_mapping` 按原始类型全名显式映射到 Wren 类型；MDL 报告保留映射记录。元数据模板仅允许一条 SELECT；执行前仍需可靠只读策略，不允许上传 Python/Java 适配实现。结果列契约：

| 模板 | 输出列 | 行含义 |
|---|---|---|
| tables | table_name、schema_name、catalog、kind、description | 替代 JDBC 表枚举，稳定标识由这些实际名称生成 |
| comments | table_id, column_name, description | 补充已扫描字段注释 |
| columns | table_id、column_name、raw_type、nullable（boolean/null）、precision、scale，可选 description | 替代 JDBC 字段扫描 |
| constraints | table_id, constraints_json | 每表一份完整约束 JSON |

约束 JSON 必须包含 `primary_keys`、`unique_constraints`、`foreign_keys` 三个数组；每个约束有 `name`、有序 `columns`，外键另有 `target_table`、有序 `target_columns`。不得逐列拆复合键。模板结果来自实际只读元数据查询，表和字段必须使用数据库真实名称；comments 和 constraints 只能引用枚举到的表/字段。替代模板未返回的字段或约束不视为已经读取，缺少标准元数据权限时，仍需管理员核实报告中的缺口。

## 边界、持久化和失效

- 凭据只在 Executor 加密卷中持久化，经 stdin 进入一次性 Java Worker，不进入 Java 命令行、模型或厂商错误日志。Worker stdout 是受限 JSON 协议；厂商标准输出和错误输出不回传。
- Worker 限制堆内存、结果行数、结果字节、超时；查询使用绑定参数，保留 DECIMAL 文本精度及原生类型信息。超过 DuckDB 数值能力的精度使用精确文本传输，并在结果摘要明确告知类型降级。
- SQL 单语句及只读 AST 校验继续生效。未知执行边界只扫描。认证、权限、网络错误保持错误分类，不触发 SQL 修复。JDBC 无无副作用 EXPLAIN 时返回明确的预检能力不可用，沿用告警后实际只读执行。
- JDBC 取消终止 Worker 及连接；驱动的语句超时约束剩余服务端工作，不宣称已经验证服务器端取消语义。
- 连接格式升级到 v2，旧原生配置、环境变量 DSN 继续读取。数据源 ID 保持不变；更换地址、数据库、账号、Schema/Catalog、URL、驱动、模式或属性使扫描/能力失效。驱动高级配置变化也使查询签名失效。
- 扫描任务状态保存在原 `scan_result` 中，无额外元数据库迁移。重启后未完成任务显示 interrupted，用户重新扫描。取消及连接编辑阻止旧结果发布；重扫按稳定表标识保留人工注释、手工关系和候选否决记录，并提供新增/移除差异。
- 大型 JDBC 扫描报告已完成表数量，原生扫描报告阶段和完成数量。单次扫描有预算，超过预算要求缩小范围。

## 接口

保留 `/datasources`、`/{id}/connection`、`/{id}/test`、`/{id}/scan`。

新增 `/datasource-products`、`/settings/database-drivers`（GET/POST/PUT）、`/datasources/{id}/capabilities`、`/snapshot`、`/validate-query`、`/scan-jobs`（POST）、`/scan-jobs/{job}/cancel`、`/scan-jobs/{job}`（GET）、`/relationships`、`/relationships/{candidate}`（PUT）、`/relationships/{candidate}/profile`、`/mdl-report` 和 `/mdl`。

`/relationships/{candidate}/ontology-candidate` 接收 `domain/source_object/target_object`，核验实际表和字段映射后提交到现有本体候选维护流程；不自动发布关系。物理关系确认用于 MDL 导出，本体业务关系继续通过本体维护保存和审核。

MDL 确定性构建按稳定标识排序，保留复合键、双向关系导航和 AND 关联条件。Wren 校验在有超时、并发和内存预算的临时子进程中运行，第三方模块异常不影响在线问数。固定 JSON Schema 来源 commit 为 `548eee3e864b11e7f11eee15ae82be178a295f22`，Wren Core 运行包单独固定为 0.8.2；分别记录格式校验、Core 构建、目标执行。国产 JDBC 不填写伪造的官方 Wren 数据源类型，`wren_target_execution` 始终保持 unverified，除非另有实际目标验证证据。

## 验收和回退

SQL 兼容策略见 [保守 SQL 能力基线](SQL_CAPABILITY_BASELINE.md)：版本号不自动启用高级语法；CTE、窗口、递归及偏移分页分别以只读探测结果为准。

契约及 API 回归：`.venv/Scripts/python.exe -m pytest`；前端：在 `apps/web` 执行 `npm run build`。

隔离合成库：

```powershell
docker compose -f deploy/compose.database-tests.yml --profile mysql up -d
$env:SEMORA_TEST_DATABASES='postgresql,mysql'
.venv/Scripts/python.exe -m pytest services/api/tests/test_database_live.py
```

这些容器使用独立项目和合成数据，不连接银行业务库。PostgreSQL 15/16、MySQL 8.0/8.4 使用独立端口；运行状态与实际通过记录以此次交付说明为准。Oracle、SQL Server 至少两个版本档位及 OceanBase 两个模式、GoldenDB、Vastbase 仍需取得实例后单独验收；不能以驱动包下载成功替代联调。

本机发布前的私有备份在 `artifacts/database-upgrade/<时间>/`，保留元数据库 dump、适用时的 Executor 配置卷（必须连同加密 key 备份），旧镜像标记为 `semora-api/executor/web:before-multidb`。本次旧容器没有配置目录/卷，已记录该情况。回退必须恢复匹配的元数据与配置卷，使用 `--no-build` 指定旧镜像；勿将私有备份加入版本库。不要删除现有持久化卷。

## 本次实际验收记录（2026-11-10）

| 产品 / 通道 | 实际版本 | 验证结果 |
|---|---|---|
| PostgreSQL 原生 | 15.19、16.15 | 合成库连接、扫描、注释、复合约束、跨 Schema 重名、绑定查询、金额精度、超时、MDL 构建通过 |
| MySQL 原生 | 5.7.44、8.0.46、8.4.13 | 合成库连接、表/视图、注释、复合约束、绑定查询、金额精度、MDL 构建通过；5.7 的 CTE/窗口探测不可用，普通查询通过 |
| 自定义 JDBC → PostgreSQL | 驱动 42.7.7；服务器 15.19、16.15 | 主包加载、结构扫描、NULL/DECIMAL/时区、查询验证、超时、主动取消、取消后新 Worker 查询、配置重读通过 |
| JDBC 高级元数据模板 | PostgreSQL 15.19 合成库 | tables / columns / constraints 替代扫描、复合键保留、驱动配置变化后查询签名失效通过 |
| Oracle / SQL Server / OceanBase | 上述固定厂商驱动 | JRE 内真实驱动类加载通过；目标数据库和兼容模式尚未联调 |
| GoldenDB / Vastbase | 未取得目标实例及厂商包 | 已提供产品、JAR/依赖、方言、只读策略、元数据模板、类型映射配置入口；产品能力待验证 |

全量 Python 回归及五个隔离数据库用例合计 **264 项通过**；前端 TypeScript/Vite 构建通过；两个真实页面用例通过，覆盖产品表单切换和损坏驱动包拒绝。另已在运行 API 中验证连接预览、异步扫描、查询验证、MDL ZIP、导出无连接密码、连接变更失效，以及 Linux 镜像内的固定 Wren 构建。

本轮临时验收数据源已从元数据库和 Executor 配置中移除；隔离验收容器在验收后停止，保留复跑配置。本机默认镜像已更新，API/Executor 运行文件与工作区逐文件哈希一致。没有访问或修改银行业务数据来构造验收结果。Oracle、SQL Server 各两个版本档位和国产产品的真实联调仍是发布验收的待完成项，不属于“已验证支持”。
