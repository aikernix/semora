# Third-party notices

Semora 使用或参考以下开源项目。名称与 Logo 不属于 Semora，也未被复制为 Semora 品牌资产。

| 项目 | 用途 | 许可证 |
|---|---|---|
| Tencent WeKnora | 通用页面布局与交互参考；可选外部知识 API | MIT |
| Canner WrenAI | 产品架构与语义模型设计参考；完整 WrenAI 产品和旧 Wren Engine 不作为运行依赖 | Apache-2.0 |
| OpenBKN BKN Engine | 仅作本体元模型概念参考；当前未复制其源码，也不是运行依赖 | 上游混合许可，未引入源码 |
| TDesign Vue Next | Web UI 组件 | MIT |
| SQLGlot | SQL AST 解析与方言转换 | MIT |
| DuckDB 1.5.6 | 进程内、临时只读查询的跨源结果关联与汇总；不增加常驻服务 | MIT |
| LangGraph | Agent 状态编排 | MIT |
| LangChain 1.4.8 / LangChain Core 1.7.4 | 工具调用 Agent 循环与结构化交付 | MIT |
| LangGraph PostgreSQL Checkpointer | 查询状态与业务澄清中断持久化 | MIT |
| Playwright | 仅用于端到端测试，不进入 Web 运行镜像 | Apache-2.0 |
| pgvector | PostgreSQL 向量类型与索引 | PostgreSQL License |
| OpenTelemetry Python / Instrumentation 1.45.2 / 0.66b2 | 跨服务 OTLP Trace 与 W3C Trace Context 传播 | Apache-2.0 |
| pgx | Go PostgreSQL 驱动 | MIT |

OpenBKN License 覆盖的文件不纳入本仓库。未来如引入任何上游源码，必须先完成逐文件许可评估，并保留原始版权头、许可证、修改说明和上游 commit。

