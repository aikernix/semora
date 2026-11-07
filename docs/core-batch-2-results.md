# 最小问数内核第二批

本地代码已完成，实验默认关闭，未部署。范围以docs/core-optimization-plan.md最后的用户收紧约束为准，不建设新UI、记忆平台、索引、缓存或独立样例库。

主要文件：app/task_memory.py、evidence_policy.py、continuation.py、agentic.py；workflow.py仅统一TTL；agent/roles.py与multisource.py补充跨源阻断/步骤条件和错误定位；planner.py拒绝跨实例直接JOIN。测试见test_task_memory.py、test_staged_protocol.py、test_source_execution.py、test_top_n.py、test_fixed_core_cases.py。

模型与数据验收：scripts/evaluate_core_model.py通过docker exec调用已配置模型，固定两SQLite数据库真实执行SQL，未访问业务数据和正式元数据。辅助bridge由tmp/model-bridge.py复制到运行容器/tmp，使用原已配置模型，未输出Key；不是生产部署。脚本--protocol baseline|staged区分工具协议，任务快照保持开启以固定变量。模型别名没有锁定供应商内部权重版本，temperature为0仍不保证可重复。

已验证：否定原请求可见；显式事实A→B→A覆盖正确；来源/冲突保留；完整复合键候选JSON往返；权限本体变化使回读失效；900秒证据边界；旧确认产物不重新标为当前已验证；精确绑定预检上下文复用；步骤筛选存在时通过、真实缺少时程序阻断。原始事实和原始记录不由摘要替代。

正确性与效率分别报告：早期模型运行正确但需约233至276秒，存在扫描元数据缺失的验收环境差异。补齐PRAGMA后最新运行在Reviewer交付超时，未执行SQL，不能作为通过。详见计划实施记录及artifacts/core-acceptance原始报告。不得拿Schema缩小、自动测试数或一次成功运行宣称整体改进已完成。


最终补充：最新核心单场景在118078ms/11次真实模型调用/2次SQLite执行下返回准确两行，符合暂定工程预算；早期失败记录均保留。全套258 passed,1 skipped。最终新增的均为现有内核最小修复（权威finish流结束、严格工具入参、服务端候选交付、中文条件边界、字段引用诊断），未增加产品页面或常驻组件。准确率与性能统计结论、生产同型链路及多场景真实模型验收仍无证据；未部署，开关默认关闭。详见计划最终验收段。
