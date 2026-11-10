# 保守 SQL 能力基线

策略版本：`portable-read-v1`。这是 Semora 的生成与验证策略，不是“所有数据库版本已兼容”的认证。SQLGlot 和 JDBC 驱动版本保持原来的固定版本。

## 默认行为

源库查询优先使用普通 SELECT、INNER/LEFT JOIN、WHERE、GROUP BY/HAVING、派生表、CASE/COALESCE 和绑定参数。聚合、筛选、排序、关联键和输出粒度不能为了兼容而被删除或改变。具体函数差异仍由 SQLGlot 转换及数据库预检/实际只读执行验证；本策略不是任意 SQL 的完整语法白名单。

CTE、窗口函数、递归 CTE、SQL Server OFFSET/FETCH 均作为可选能力。默认不启用；只有对应的只读探测返回预期值，才启用该项。普通 CTE 通过不能替代递归 CTE 验证，TOP 通过也不能替代 OFFSET/FETCH 验证。版本号仅作描述，不能把未测试能力自动置为可用。失败、权限不足、超时和未知都不作为“已支持”的证据。

查询理解、SQLProgram、SQLGlot、Executor 的流程顺序、模型角色及提示词文件不变。模型看到的目标能力上下文采用上述保守值。未启用的语法在既有 SQL 校验/局部修复链路处理；不能保持原意时明确报告，不通过删除窗口、过滤、排序或偏移来强行交付。

本地 DuckDB 步骤继续使用它自身的能力；跨源中间结果的完整性和行数限制保持原有规则。

## 分页

| 目标 | 默认物理 SQL 形状 | 限制 |
|---|---|---|
| PostgreSQL / MySQL | LIMIT | 可选偏移按实际探测记录 |
| Oracle / OceanBase Oracle 模式 | 排序放在内层，外层 ROWNUM 限制 | 默认不要求 FETCH；拒绝无法忠实转换的 OFFSET、WITH TIES、PERCENT |
| SQL Server / 显式配置的 TSQL | TOP | OFFSET/FETCH 须另行探测通过 |

驱动配置 `pagination=baseline` 是默认值。管理员可显式选择 `sqlglot` 或 Oracle 的 `rownum`；`scan_only` 保持关闭问数。显式选择不会绕过查询验证或自动证明目标版本支持该语法。查询编译和关系采样复用同一个分页转换函数。

Oracle 的排序子查询与外层 ROWNUM 依据 [Oracle 文档](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/ROWNUM-Pseudocolumn.html)；SQL Server TOP 与偏移分页分别参考 [Microsoft 文档](https://learn.microsoft.com/en-us/sql/t-sql/queries/top-transact-sql)。这些是语法依据，不替代历史版本实例验收。

## 探测、缓存与失效

用户点击“验证查询”时，原生 MySQL/PostgreSQL 和 JDBC 都执行有超时的只读合成探测，不采样业务数据。探测检查实际返回值，偏移探测验证跳过第一行，递归探测验证生成第二行。正常问数不重复探测，因此不增加每道问题的模型轮次或探测往返。

能力结果记录 `sql_baseline`。JDBC 验证签名绑定当前策略和有效分页配置；升级后的旧验证需重新执行。旧 JDBC 来源页面显示待验证，不能靠旧缓存继续宣称查询已验证。原生旧来源保留基本查询入口，高级能力仍按实测证据启用。

重新测试发现产品、服务器版本、驱动版本或兼容模式变化时，清除旧的探测证据，并标记结构需重新扫描。相同目标的普通重扫可保留仍有效的探测结果。

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

升级前备份及镜像位于 `artifacts/database-upgrade/*-sql-baseline/` 和 `semora-{api,executor,web}:before-low-sql-baseline`。无需元数据库结构迁移。
