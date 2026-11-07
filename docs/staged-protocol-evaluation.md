# 阶段化协议：第一批验证

状态：本地实现，默认关闭，未部署，真实模型对比未完成。启用环境变量AGENT_STAGED_PROTOCOL=true。只作用于Analyst，Reviewer仍使用独立上下文和完整方案。没有引入Wren源码依赖。

阶段：exploring允许只读探索、enter_planning和精简交付契约（仅candidate_id/summary/clarifications）；planning仅validate_plan、resume_exploration、交付；delivering仅候选交付或返回探索。合法查询方案只能通过当前阶段的服务端候选交付。最终仍重新构建并校验完整方案，不使用模型摘要替代执行条件。未经提供的工具调用由runtime拒绝。

失败：确定性校验失败保持planning，允许继续修复；需查证时显式resume_exploration；已有工具事实不截断。阶段不会增加Agent数量，也不会绕过Reviewer/SQL门禁。新的分析阶段清除候选；绑定现已包含问题、完整本体、权限范围、授权上下文、需求和阶段ID。

固定契约测量：services/api/tests/test_staged_protocol.py使用generic域record对象、固定amount>10条件。原始工具Schema28084字节，探索5210，规划14335，交付1936（UTF-8紧凑JSON）。工具定义之外的消息没有在该测量中压缩；不能把这些差额当成整次任务总请求减少量。探索切到规划增加一次模型往返，本地模拟为3轮（enter_planning、validate_plan、交付）；原直接校验路径为2轮，因此默认关闭，先做真实模型对比。

命令：.venv/Scripts/python.exe -m pytest services/api/tests -o addopts='' -q：233 passed,1 skipped（添加最后一个阶段端到端测试之前）；后续test_staged_protocol.py单独运行2 passed。已有固定fixture覆盖单表、NULL、Top N、聚合、预聚合、跨源、续聊和超时取消；这是契约/确定性回归基础，不是冻结生产数据和模型参数的完整P0真实评测集。

未完成：真实模型延迟/调用数/正确率对照；任务记忆持久化与重启恢复；局部错误窄上下文；按规模结构召回；成功查询记忆；缓存测量。本文件仅记录第一批，不代表整个计划实施完成。
