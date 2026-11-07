# 51talk 导入与复合键

业务域 `talk51` / 显示名 `51talk`，版本 `1.0.0`。只导入六个对象、六条关系，知识库绑定为空。订单对象、订单关系、动作和订单指标均排除。

## 配置与导入

四个只读连接 URL 仅放入本地忽略的 `.env`：`DATASOURCE_TALK51_{CRM,APPOINT,LOG,CONTENT}_URL`。Compose 只将这些密钥注入 Executor；API 和 Agent 使用密钥引用。

启动六个 MVP 服务后运行：

```powershell
.venv\Scripts\python.exe scripts/import_talk51_domain.py "C:\Users\Administrator\Desktop\体验课用户转化分析.json" --publish
```

脚本通过 Executor 核对实际库名、只读权限和所需物理字段，只扫描 `user_call_detail`、`appoint_hot`、`remark_log`、`appoint_audio`、`appoint_audio_text`。限定扫描接口为 `POST /api/v1/datasources/{id}/scan?tables=table1&tables=table2`，目前支持 MySQL。

核验失败保留本地导入报告和可审阅草稿，不发布。重复发布已有业务域会停止，不覆盖不可变版本。导出目录 `artifacts/talk51/1.0.0` 被 Git 忽略，含本体、快照、四份 Wren 产物、哈希清单及报告。银行发布状态与草稿前后比对必须一致。

## 通用执行支持

- `projection.distinct: true` 仅对声明的物理列去重，生成受限 `refSql`；本地 SQL、预聚合和 Wren 路径保持相同语义。学员只暴露 `appoint_hot.s_id`。源库不建视图，不执行写入。
- `StepInput.key_pairs` 替代单键 `source_field` / `target_field`，两种格式互斥。旧单键查询继续兼容。
- 关系只能是字段等值映射，由 AND 连接。验证全部字段、数据源、类型、敏感等级以及完整性。
- 复合键按元组去重，任一分量 NULL 的元组跳过。绑定谓词为 `(a=:a1 AND b=:b1) OR (...)`，禁止拆成独立 IN。最多 1000 个元组，SQL 最长 100000 字符，仍受现有超时和结果限制。
- 空键集合的明细查询跳过；全局汇总执行恒假过滤，让数据库保留 COUNT=0 / SUM=NULL 语义。
- 同源关系进入各自 MDL；跨源关系仅作为顺序执行契约。审批哈希和审计绑定完整计划与实际参数。Reviewer 看到全部依赖字段，不接收中间原始数据。

未确认的接通、完课、时间单位等口径继续由用户澄清。`use_point=free` 只是体验课定义，不是全域默认过滤。按用户要求，51talk 自 1.0.1 起全部属性不标记敏感（sensitive=false，sensitivity=internal）；导入脚本同样遵循此配置。旧发布快照保留原分类。此变更不影响其他业务域。
