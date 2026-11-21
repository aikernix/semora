# Semora 离线建模

以一份数据库结构 DDL、零份或多份文档建立虚拟数据源。确认导入后固定文件和表目录，关联仍可新增、编辑、确认、拒绝、停用和恢复。数据存放在扩展自己的 SQLite 文件和文件卷。

```sh
docker compose -f extensions/offline-modeling/compose.yml up --build -d
```

主站默认开启离线数据源，运行 `docker compose up -d --build` 会一并启动离线建模服务。若要关闭主站离线功能，在仓库根目录的 `.env` 设置 `OFFLINE_MODELING_ENABLED=false` 后运行 `docker compose up -d --build api web`。入口向可管理数据源的用户显示。单独运行离线建模时，打开 http://127.0.0.1:8015/ 。

主站启用身份认证时，离线扩展需要通过 `OFFLINE_ACCESS_PASSWORD` 配置独立 Basic Auth（用户名 `offline`）。单独运行的扩展默认发布到本机回环地址。主站集成和单独运行各自使用独立数据卷，已有离线资料可复制到目标数据卷。

- 选择数据库产品，按导出语法选择兼容模式。解析覆盖 MySQL、PostgreSQL、Oracle、SQL Server 四套兼容语法。产品列表包含 OceanBase、GoldenDB、海量数据库、达梦、人大金仓、openGauss 和 GaussDB 等。各厂商的专有语法以实际 DDL 样本为准。
- 结构 DDL 支持 `.sql`、`.ddl`，文本使用 UTF-8。首版支持常见 CREATE TABLE、ALTER TABLE ADD CONSTRAINT、CREATE INDEX 和注释。简单视图保留定义和显式输出名称，复杂结构语句返回诊断。
- 资料支持 XLSX、DOCX、TXT 和 SQL。导入时保留原文、表格布局和位置，发布 DDL 外键。文档关系在手动关联分析时提取。明确的字段对应及必要条件可用 DDL 交叉校验，历史 SQL 中可确定的简单物理 JOIN 也可提取。基数未知时进入待确认。
- 上传总大小 64 MiB，单条 DDL 语句上限 2 MiB。失败可用同一批文件重试。服务按单 worker 启动，重启后重跑中断的导入。
- 手动分析时，模型先逐批编目，为任意布局记录区段用途、原文范围和需要合读的位置。随后按目录回读正文和锚点，各格式共用一套语义提取契约，输出明确声明、历史 SQL 和业务线索。原文引用必须能回定位，表、字段、键和条件用已导入 DDL 复核。关联发布时保存文档目录和覆盖统计，无法定位的内容留在导入详情的 `document_analysis` 缺口中。业务线索作为常规关联比较的证据。
- 配置 `OFFLINE_MODEL_URL`（OpenAI 兼容接口基地址，通常含 `/v1`）、`OFFLINE_MODEL_NAME` 及可选 `OFFLINE_MODEL_KEY` 后，可运行补充关联分析。每批最多 10 份精简表结构用于提取关键词。对表名、字段名和注释忽略大小写与空白进行全目录召回，合并相同命中集合，跳过不足两张表的结果。超过 20 张表的组由模型整理。整理后的组独立分析，大组和宽表分批读取资料并收尾，最后汇总核查候选。
- 离线核查使用固定 DDL、约束、注释和文档。字段注释明确指向目标列、映射覆盖目标唯一键且没有附加条件的推断可自动生效。仅能由共同主体解释的提议被排除。尚缺必要证据的提议进入待确认并显示缺口。取证无需连接数据库。配置模型地址即指定资料处理服务。
- SQLite 保存已完成模型请求的检查点。失败或取消后的重试可复用相同输入的答复，生效关系在发布前继续可用。成功发布后清理检查点，下次完整分析重新提取。
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
| `RELATIONSHIP_AGENT_GROUP_THRESHOLD` | 40 | 超过该表数进入自主分析，宽表也可因请求体积走自主分析 |
| `RELATIONSHIP_AGENT_MAX_STEPS` | 8 | 每组自主分析的模型调用轮次上限，包括收尾 |
| `RELATIONSHIP_MAX_MODEL_CALLS` | 2000 | 单次任务的模型请求预算 |
| `RELATIONSHIP_RESPONSE_FORMAT` | `json_object` | 可设为 `text` 以省略请求的响应格式参数 |
| `LLM_ENABLE_THINKING` | `false` | 默认关闭模型推理 |
| `RELATIONSHIP_THINKING_MODE` | `inherit` | 继承模型配置，也可显式设置 `true`、`false` 或 `omit` |

默认使用流式响应，开始收到可见答案后停止未交付计时，单次请求总时限仍持续。关闭流式时，收到完整响应才算交付。每次请求独立计时，排队与其他批次不占用本次时限。关联分析与在线流程一样分两层重试。HTTP 层按 `LLM_REQUEST_RETRIES` 重试请求总时限或读取超时，交付超时交由阶段处理。提词、分组、比较和审核最多三次阶段尝试。Agent 探索每轮一次阶段尝试，默认最多五轮探索，余量留给最多三次收尾。工作台查询共用主流程的 `LLM_NO_DELIVERY_SECONDS`，默认 60 秒。

自动化测试可在扩展目录安装 requirements 后运行 `python -m unittest discover -s tests -v`。
