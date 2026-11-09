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
| pypdf | PDF 文本解析 | BSD-3-Clause |
| pypdfium2 / PDFium | 扫描 PDF 页本地渲染 | BSD-3-Clause / Apache-2.0 及 PDFium 第三方许可 |
| Tesseract OCR / pytesseract | 中文、英文图片和扫描文档本地 OCR | Apache-2.0 |
| python-docx / python-pptx / openpyxl | Office 文档解析 | MIT |
| Beautiful Soup | HTML 文本解析 | MIT |
| Boto3 | S3 兼容对象存储客户端 | Apache-2.0 |
| SeaweedFS | Docker 演示环境的独立 S3 兼容对象存储服务 | Apache-2.0 |
| OpenTelemetry Python / Instrumentation 1.45.2 / 0.66b2 | 跨服务 OTLP Trace 与 W3C Trace Context 传播 | Apache-2.0 |
| OpenTelemetry Collector Contrib 0.137.0 | Docker 演示环境的 OTLP 接收、批处理与调试导出 | Apache-2.0 |
| pgx | Go PostgreSQL 驱动 | MIT |

OpenBKN License 覆盖的文件不纳入本仓库。未来如引入任何上游源码，必须先完成逐文件许可评估，并保留原始版权头、许可证、修改说明和上游 commit。

