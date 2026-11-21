# Semora 离线建模

以一份数据库结构 DDL、零份或多份文档建立虚拟数据源。确认导入后固定文件和表目录，关联仍可新增、编辑、确认、拒绝、停用和恢复。数据存放在扩展自己的 SQLite 文件和文件卷。

```sh
docker compose -f extensions/offline-modeling/compose.yml up --build -d
```

主站默认开启离线数据源，运行 `docker compose up -d --build` 会一并启动离线建模服务。若要关闭主站离线功能，在仓库根目录的 `.env` 设置 `OFFLINE_MODELING_ENABLED=false` 后运行 `docker compose up -d --build api web`。入口向可管理数据源的用户显示。单独运行离线建模时，打开 http://127.0.0.1:8015/ 。

主站启用身份认证时，离线扩展需要通过 `OFFLINE_ACCESS_PASSWORD` 配置独立 Basic Auth（用户名 `offline`）。单独运行的扩展默认发布到本机回环地址。主站集成和单独运行各自使用独立数据卷，已有离线资料可复制到目标数据卷。

- 选择数据库产品，按导出语法选择兼容模式。解析覆盖 MySQL、PostgreSQL、Oracle、SQL Server 四套兼容语法。产品列表包含 OceanBase、GoldenDB、海量数据库、达梦、人大金仓、openGauss 和 GaussDB 等。各厂商的专有语法以实际 DDL 样本为准。
- 结构 DDL 支持 `.sql`、`.ddl`，文本使用 UTF-8。首版支持常见 CREATE TABLE、ALTER TABLE ADD CONSTRAINT、CREATE INDEX 和注释。简单视图保留定义和显式输出名称，复杂结构语句返回诊断。
- 资料支持 XLSX、DOCX、TXT 和 SQL。表格中的「关联关系」「关联条件」「外键表达式」列及正文 `关联关系：表.字段 = 表.字段` 可提取为声明。支持由 AND 连接的复合字段和等值常量条件。历史 SQL 中可确定的简单物理 JOIN，基数明确时直接生效，基数未知时进入待确认。保留可解析的等值条件和原文。完整主键或唯一键可提供基数依据，两端都缺乏唯一性依据时需要明确的历史标注或人工确认。解析不明的声明提示人工处理。
- 上传总大小 64 MiB，单条 DDL 语句上限 2 MiB。失败可用同一批文件重试。服务按单 worker 启动，重启后重跑中断的导入。
- 配置 `OFFLINE_MODEL_URL`（OpenAI 兼容接口基地址，通常含 `/v1`）、`OFFLINE_MODEL_NAME` 及可选 `OFFLINE_MODEL_KEY` 后，可运行补充关联分析。模型收到本批表结构、已知映射和相关文档片段。字段注释明确指向目标列、映射覆盖目标唯一键且没有附加条件的关系可自动生效。可由同一主体路径解释且没有独立直接引用依据的提议会被排除。其他有依据的推断进入待确认，并显示需核查的内容。配置模型地址即指定资料处理服务。
- 主站通过数据源页的弹窗创建，表结构与关联使用统一抽屉。解析文件后完成导入，模型关联分析须手动启动，可在关系抽屉中取消。删除数据源会删除其离线文件、目录和关系。
- 在本体工作室将已导入的数据源绑定到业务域。每个业务域可绑定一个离线数据源，或绑定一个或多个在线数据源。工作台按业务域选择查询路径，仍使用主站的模型设置、会话记录、知识库检索、口径确认和 SQL 生成。离线 SQL 在本地编译完成后交付。
- 退出单独运行的扩展使用 `docker compose -f extensions/offline-modeling/compose.yml down`，数据卷默认保留。

离线建模采用与在线关联建模一致的模型配置。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `LLM_TIMEOUT_SECONDS` | 300 | 单次模型请求总时限，同时用于 HTTP 超时 |
| `RELATIONSHIP_NO_DELIVERY_SECONDS` | 300 | 关联分析单次等待模型交付可见答案的时限 |
| `LLM_REQUEST_RETRIES` | 1 | 单次请求失败后的重试次数，范围 0–2 |
| `RELATIONSHIP_MODEL_CONCURRENCY` | 3 | 每个分析任务的并发请求数，范围 1–3 |
| `RELATIONSHIP_RESPONSE_FORMAT` | `json_object` | 可设为 `text` 以省略请求的响应格式参数 |
| `LLM_ENABLE_THINKING` | `false` | 默认关闭模型推理 |
| `RELATIONSHIP_THINKING_MODE` | `inherit` | 继承模型配置，也可显式设置 `true`、`false` 或 `omit` |

默认使用流式响应，开始收到可见答案后停止未交付计时，单次请求总时限仍持续。关闭流式时，收到完整响应才算交付。每次重试独立计时，排队与其他批次不占用本次时限。工作台查询共用主流程的 `LLM_NO_DELIVERY_SECONDS`，默认 60 秒。

自动化测试可在扩展目录安装 requirements 后运行 `python -m unittest discover -s tests -v`。
