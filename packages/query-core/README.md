# Semora QueryCore

无 Agent、HTTP、数据库或模型依赖的确定性规划包。API 和 Wren 服务均加载同一份代码；原 `app.planner`、`app.query_stages`、`semora_agent.multisource` 保留兼容导出。

- `intent.py`：从 QueryIntent 解析业务关系角色、粒度、预聚合范围和源拆步。业务主体不等于技术首个数据源，必要的跨源筛选由程序前置。
- `sources.py`：源内投影、完整等值关联键、类型/敏感标识和步骤依赖校验。
- `stages.py`：聚合粒度、关联放大、COUNT 零值与 SUM/AVG NULL 语义证明。
- `sql.py`：生成参数化语义/物理 SQL，保留既有 Wren 编译与方言适配边界。

复用真实的 QueryIntent、QueryPlan、SourceQueryPlan、QueryStage 和 StepInput 契约，没有另建空的多层 DTO。当前支持的是已证明可执行的计划，不是任意联邦查询引擎。

Docker 镜像将 `python/semora_query_core` 复制到 `/app/semora_query_core`。本地测试路径已配置在根 pyproject.toml；脚本可将 `packages/query-core/python` 加入 PYTHONPATH。依赖现有 SQLGlot 和 semora_contracts，没有新增第三方依赖。
