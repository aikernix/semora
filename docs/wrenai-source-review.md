# WrenAI源码调研与Semora改进

参考代码：D:/Develop/WrenAI，main提交548eee3e864b11e7f11eee15ae82be178a295f22；旧版D:/Develop/WrenAI-legacy，legacy/v1提交ce9ed54（完整提交以git rev-parse HEAD为准）。只读调研，不把参考项目作为运行依赖。

## 查到的实现

1. 新版 core/wren/src/wren/ask.py 的render仅生成提示模板，不自行运行一个问数模型。Agent由外部宿主提供，不能把新版CLI等同旧版聊天产品。
2. 旧版 wren-ai-service/src/web/v1/services/ask.py：默认最多5项历史，先分类/召回SQL样例与指令，再检索结构，生成SQL并进行规划/试运行。错误修正传入相关结构、错误SQL、具体错误，最多3次；TIME_OUT中止修正循环。限制历史条数不是无损压缩，不能直接照搬到需求确认记录。
3. 旧版 pipelines/retrieval/db_schema_retrieval.py包含表/字段选择；pipelines/generation/sql_correction.py单独处理SQL错误。它仍然使用JSON输出，不能把我们超时简单归因于JSON。
4. 新版 core/wren/src/wren/memory/store.py:get_context：小Schema完整描述，大Schema按语义检索，检索附manifest哈希避免混用不同模型版本；recall_queries默认3条历史问答SQL并可按数据源筛选。
5. docs/core/concepts/memory_system.md：业务规则与问答SQL的持久来源是knowledge目录，LanceDB为可重建索引，另有词法检索后备。没有发现一个能直接移植的“整段ReAct聊天无损摘要器”。
6. 许可：main的core/sdk/skills为Apache 2.0，docs为CC BY 4.0；legacy/v1根LICENSE为AGPL-3.0。旧版行为可用于架构分析，不直接复制实现。本次Semora代码独立编写。

## 与Semora故障的对应关系

- 当前900秒故障是在模型尚未提交工具调用时发生。检索记忆和缩减协议可以减少输入负担，但不能保证解决模型内部生成不收敛；需要同问题、同模型、同权限快照的受控对比。
- 当前已存在按对象读取与精确重复观察引用，不需要再引入盲目摘要。用户修正、定义冲突、完整关联条件、权限和版本必须保存。
- 我们的SQL表达限制在QueryPlan层，应逐项补齐必要能力；不能因Wren接受SQL就绕过门禁改成任意表达式。

## 本次实际修改

validate_plan成功返回candidate_id+plan_summary，而非回传完整QueryPlan。服务端仍保存完整候选；最终引用时重新验证。字段、过滤、复合键、版本与审批机制没有改变。减少一次完整方案回声，Reviewer仍取得完整独立方案。新增messages_bytes与tool_schema_bytes分别统计，保留原有总预算及超限停止机制。

## 后续优先级（尚未实施）

P1：将探索工具定义与最终交付协议做阶段化收窄，避免每轮携带全部复杂执行结构；与现有协议做相同问题集正确率/耗时对比。
P1：建立最小结构化任务记忆：已确认事实、冲突、证据引用、候选ID及未解决问题；历史工具全文按ID回读，不用自由文本摘要替代事实。当前候选仅阶段内有效，跨运行恢复必须重新校验。
P2：已验证查询样例库。复用前严格按业务域、本体哈希、数据源和权限筛选；只有复核通过且执行成功的样例可成为候选，不能把失败草稿当成功记忆，也不能复用旧参数/审批。
P2：大目录按名称/别名/语义召回对象，补齐关系连通路径及键字段；检索没命中时保留浏览全目录的工具，防止召回遗漏降低准确率。
P3：错误修正使用窄上下文：当前方案、明确问题、相关定义与用户确认。业务口径Reviewer只在定义变化时重查；结构性正确性由程序校验，不能用减少复核次数替代正确性保证。

验收同时看最终结果正确率、确认条件遗漏率、关联膨胀风险、首次工具输出延迟、请求体大小、模型调用数和超时率。不能只以token少或速度快判定优化成功。
