# 模型请求诊断

适用于 Analyst、Reviewer 的工具调用，以及需求理解、SQL解释和结果说明的 JSON 调用。

默认开启 `LLM_STREAM_ENABLED=true` 和 `LLM_STREAM_INCLUDE_USAGE=true`。兼容接口若不支持 usage 请求选项，可关闭后者；不支持流式可关闭前者。模型服务忽略流式并返回 JSON 时，自动按 JSON 解析。400 等配置错误不会自动重试或悄悄切换协议。

每个尝试有独立 `diagnostic_id`，同时写入 Docker API 日志和 Agent 事件（JSON辅助调用仅写 Docker 日志）。保留服务端 request ID 和响应 ID，便于供应商定位。后台每15秒记录一次进度，部分响应不执行工具；完整结束后才交给 Agent。

字段含义：

- `tcp_connected_ms`、`tls_ready_ms`、`request_sent_ms`：HTTP传输阶段相对于请求开始的时间，传输实现未报告时缺省。
- `headers_ms`：收到响应头。此前的延迟可能包含网络、排队或服务端计算，不能单独认定为推理耗时。
- `first_event_ms`：首个完整模型响应事件；不是精确的首字节时间。
- `first_reasoning_ms`、`first_output_ms`：首次观察到推理片段、答案或工具参数的时间。二者差值不能当作精确的模型内部计算时间。
- `reasoning_chars`：服务端显式返回的推理文本字符累计数；正文立即丢弃。不提供推理片段的模型可能始终为0，0不代表没有内部推理。
- `output_chars`、`tool_argument_chars`：答案正文、工具参数字符数。
- `prompt_tokens`、`completion_tokens`、`total_tokens`、`reasoning_tokens`：仅接受服务端 usage 的数字白名单；未提供即不记录，不能推算成0。completion_tokens是否包含推理token由供应商定义，不能直接相加。
- `stage`：waiting_headers / waiting_body / receiving_reasoning / receiving_output / completed。
- `timeout_kind`：read_idle（读取无进度）或request_deadline（整次请求截止）。整次请求仍受300秒上限；Agent整体受900秒上限。上层取消记录 `agent.request.cancelled` 和最后进度。

可用 `docker logs semora-api-1 --since 20m` 查看 `agent.request.*`。页面的 Agent 多轮分析面板显示阶段摘要与周期进度；审计事件保留详细计数。未记录密钥、请求URL、原始请求、模型正文、工具参数值和推理正文。

诊断证据：推理字符持续增长而输出尚未出现，说明服务在返回推理片段；只停在waiting_headers不能证明模型内部推理。非流式 JSON 的首次片段时间均在完整响应后观测，不能用来还原生成过程。旧任务缺失的指标无法补录。
