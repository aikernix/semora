# 真实服务内核验收

2026-11-05。使用本地现有六个MVP服务、真实qwen3.7-max、已发布cross_source_demo 1.0.0、实际Wren Core、Executor，以及登记的只读MySQL和PostgreSQL。独立SQL只读核对结果，不输出原始行或凭据。不使用SQLite或mock替代本批真实链路。

## 配置与边界

仅隔离验收Python子进程启用AGENT_STAGED_PROTOCOL=true、AGENT_TASK_MEMORY=true；ALLOW_LOCAL_PLANNER_FALLBACK=false。产品默认开关仍false，安装包未替换，不等同部署。模型单请求300秒、Agent总时限900秒及Reviewer、权限、执行门禁保持。验收会话标记为[验收]，会写入本机元数据库审计记录；业务库仅SELECT。

固定真实Wren TopN预检结果与独立MySQL SQL完全一致（3行）。PG取2个关联键、MySQL取关联明细的独立参考结果8行。独立参考并不等于模型跨源端到端通过。

## 修订v2结果

来源：/tmp/semora-live-acceptance-v2，新进程加载独立代码快照。结果保存在artifacts/core-live-acceptance/v2-runs.json。

- case1 TopN：completed，159136ms，16次模型请求，3行与独立SQL及稳定参考完全一致。
- case2 用户只改数量为2：failed，50387ms，7请求。候选order_by=[]、limit=null。Reviewer准确指出/order_by、/limit缺失；修订后的执行字段仍未变化，门禁停止，未执行错误方案。
- case3 跨源：failed，130650ms，13请求，同样在修订执行字段未变化处安全停止。不能宣称跨源模型链路通过。
- case4重复场景尚未完成，主动停止自己的旧版本验收进程，改用最新代码重新测；没有停止用户任务或常驻服务。

此前最初进程的一次Reviewer请求：推理字符1270停止增长，工具参数持续增加到10万字符以上，未结束。没有原始参数留存，因此尚不能认定内容重复或重复JSON键。新增只记结构计数的诊断用于区分，不保存隐藏推理或字段值。

## 针对修复

1. 无pending findings时，从实际对外Schema彻底移除resolutions及其items结构；有pending时才带完整解决协议。理由、问题数量及长度受限，不能把64K保护上限称为收敛通过。
2. 参数超限丢弃整个未终止产物，只允许一次限定精简交付，仍完整校验和复核。新增重复块、已知键次数、JSON有效性和重复键数，仅日志计数。
3. 方案预检非通过必须给出定位、证据和验收条件，残缺一句话不能直接驱动Analyst重做。
4. 原流程validate_plan只检查结构，未修改的修订也可获得valid回执，程序立即交付后才在外层判停。现将同一check_revision前置到回执生成前，保持外层门禁，让模型在当前修订阶段拿到具体未修改路径并有限修复；不再授予错误成功回执。
5. 完成会话的已确认上下文原本嵌在request序列化文本中，记忆仅对失败续聊/当前requirement建引用。现将同会话、同业务域、相同授权的上次确认执行字段显式投影到task_memory，包括排序、数量、筛选；标明历史且必须重验、本轮用户修改优先，不自动把旧数量盖过新数量。

## 最新v3状态

新快照在/tmp/semora-live-acceptance-v3，报告/tmp/semora-live-results-v3。重新运行相同四个场景，未完成项保持未验收。进程仅调用真实现有服务，不调整模型/Reviewer要求、不提高超时。

更新最终摘要时必须注明每条结果、独立SQL比较、耗时和模型次数，不能用预置计划、SQL预检或单元测试替代模型交付。

### v3进一步定位

首条69738ms/9请求后安全失败，未执行。Analyst已真正补齐order_by与limit（从公开validate_plan参数核实），不再是执行字段未变；失败转移到Reviewer遗漏必需的resolutions。三次结构化输出的resolution_keys均为0、duplicate_json_keys均为0，参数约287字符，不能再把这次故障解释为参数膨胀或重复内容。原契约把resolutions定义为可选，运行时却要求覆盖全部structured pending IDs，存在明确契约不一致。

修复：有pending时resolutions在对外Schema中必填，最小数量覆盖structured问题，finding_id仅允许当前已知ID枚举；仍由服务逐项校验无遗漏、重复、编造以及resolved状态。无pending时仍不提供该字段。错误反馈明确给出遗漏ID。新增Schema正反例通过。没有弱化复核。

### v4定点复验（运行中）

来源/tmp/semora-live-acceptance-v4；报告/tmp/semora-live-results-v4。复用v2真实查询成功的同一验收会话82552e86-f0da-4087-8efb-46438f22ca2d，直接发“只改为前2条、其余不变”，避免反复重跑首个需求掩盖续聊缺口。最新查询a2db19c9-0a32-4121-9c8a-e65f5161177e。此前失败轮次完整保留，未清空历史。仍和独立真实MySQL SQL比较2行及排序、筛选，保留全部门禁。v3自己的旧验收进程已停止，不算其跨源或重复场景通过。

v4代码包SHA256：153c9f8fd8995ed2e538d7d2c6cbbdd076eae3daa787731f3f778b7afac0b251。v3包ae9b22adb407720412ddb3af539af764af2116abbce8976fc765968649678369。均为本地临时代码快照，无凭据。最新自动回归264 passed、1 skipped，跳过仍为未配置TEST_METADATA_DATABASE_URL；不代替真实模型验收。

### v4–v6恢复链路定位

v4进入clarifying（56295ms/6请求）：失败的首次修改run.requirement为空，handoff只携带失败修改本身，遗漏更早成功Top3的口径。不是数据库连接问题。v5已将历史确认显式持久写入task_memory；v6进一步让同一历史确认传给独立Reviewer，默认memory=false路径也有测试。保留同会话、同业务域、同授权筛选；新需求边界不继承之前的成功口径；历史版本一律标注重验，不继承SQL/审批。

v5仍在75061ms/9请求后失败，v6也未交付。终止后公开工具参数结构证明：模型连续遗漏analysis.order_by/limit，即便说明写出排序和前2条，旧工具Schema仍将二者设为可选。不是参数被程序截断：本批完整响应duplicate_json_keys=0，无隐藏推理留存。修订门禁成功拒绝了没有修改执行字段的回执，没有执行无排序/无限制方案。

最小契约修复：阶段式validate_plan对外Schema要求显式给order_by/limit，并在服务端核实确实提供；无排序用[]，无业务TopN用null。默认旧协议兼容保留。字段不可只写在说明文字。没有从自然语言正则硬凑SQL或默认替用户选口径。

### v7最新定点复验（运行中）

/tmp/semora-live-acceptance-v7，报告/tmp/semora-live-results-v7，仍复用原会话且所有失败轮次保留。查询b0714071-28a3-4fe2-8fcf-87df1b4ba784。首个候选公开参数已明确order_by=records.record_id desc、limit=2；已保留is_not_null和两个输出字段。方案预检直接approved，正在执行前复核，尚未据此宣称执行通过。

本地回归267 passed、1 skipped。成功Top3→修改失败→再次续聊的状态传递、显式新需求隔离、memory=false/true时Reviewer历史可见性均有测试。公开参数仅记录字段名、排序结构和数量，未记录筛选值、业务行、完整SQL或隐藏推理。既有模型超时/总时限不变。

### v7续聊已通过

89596ms，9次模型请求，completed。两行结果与前后稳定的独立MySQL SQL完全一致，result_correct=true、limit_correct=true、order_preserved=true。保留非空过滤、两个输出字段及降序，数量改为2；未清空历史、没有绕过独立方案预检/执行前复核/结果复核。真实Wren编译，真实Executor执行，无本地规划后备。

代码包SHA256 ffecca577d753e01ef32fa2be18fdcf6e87c92ee87f5c6c290c722738b7cdde6。结果与公开参数结构已复制artifacts/core-live-acceptance/v7-followup-runs.json及v7-followup-payload-shapes.jsonl。此为一次通过，不是全场景成功率或严格A/B性能结论。

当前用相同v7产品代码独立核验跨源场景；尚未完成前不宣称通过。该次只更换验收问题及报告目录（/tmp/semora-live-results-v7-cross），不改服务逻辑或配置。

### 跨源同类契约遗漏

v7跨源Reviewer正确指出第一步query.order_by/limit遗漏。该版只对根方案强制显式填写，嵌套步骤仍可遗漏；未执行此方案。停止自己的旧验收进程后，v8将相同要求递归覆盖各源query，并在服务端验证各步骤确实提供字段，不以说明文字替代。

v8新进程/tmp/semora-live-acceptance-v8/cross_acceptance.py，报告/tmp/semora-live-results-v8-cross。仍是实际PG取2个键→MySQL明细（独立参考8行），保留全部权限/复核/参数化键传递门禁。没有放大SQL能力范围或引入额外服务。所有旧失败和停止记录均保留。

### v8实际服务验收已通过

- 跨源44aec438-4775-4aa0-bd57-644ba9ca9b81：completed，153412ms、12模型请求、2个source_steps、8行，与前后稳定的独立PG→MySQL SQL结果逐行（保留重复行）一致。全部Reviewer阶段正常通过。
- 重复续聊3de672fd-dded-46eb-927b-7b43bc0e74cd：completed，83035ms、9模型请求、2行，与独立MySQL SQL一致，降序、非空筛选和limit=2正确。
- v8代码包SHA256 c53d01696cb361c94ae710d13ab03413a283989fc4fba6c100bb8091adaa6957；API回归267 passed、1 skipped。样本数量有限，不宣称统计成功率或严格A/B性能提升。

报告/事件/公开参数结构已复制到artifacts/core-live-acceptance/v8-cross-*及v8-repeat-*，没有原始行、密钥或隐藏推理。旧失败证据未删除。

## 本机API更新准备

以上验收通过后才构建/更新。旧API镜像sha256:ca1ea4abab3cd2fde6e4325f43afb13a1f00fcb5407beff0fe4a6757029f1b6b，已标记semora-api:rollback-core-20261105；非敏感回退配置保存在artifacts/core-live-acceptance/rollback.json（旧进程无两项开关，等价默认false；模型300秒、Agent900秒）。只更新本机API，不更新外部生产、不增加常驻组件、不改业务数据。待构建完成后用既有update-mvp.ps1 -Services api；该脚本--no-build，因此先独立build。

## 本机更新已完成，HTTP验收进行中

先docker compose -f docker-compose.yml -f deploy/compose.mvp.yml build api，再scripts/update-mvp.ps1 -Services api。新镜像sha256:0ff6bf78979998cff50a8f2cfcac7fa74f9c658acb276994c263642413a873d0。

容器实际环境核实：AGENT_STAGED_PROTOCOL=true、AGENT_TASK_MEMORY=true；LLM_TIMEOUT_SECONDS=300、AGENT_TIMEOUT_SECONDS=900未改。仅本机.env设置两项已有开关，不改仓库默认值false。API /healthz正常，实际AsyncPostgresSaver；仍只有原六个服务，其他容器未重建。

更新后通过真实HTTP POST /api/v1/sessions及/messages创建[验收]更新后真实HTTP核心查询，收到202后轮询GET /runs/{id}，再与真实Executor上的独立MySQL SQL核对；不直接调用w.start冒充HTTP验收。报告将落在artifacts/core-live-acceptance/http-core-smoke-report.json。业务数据仅只读，所有原始行只在比较进程内存中使用。

### 更新后HTTP验收已通过

f0825190-5ae5-47ef-8d81-298851db81c3，completed，108788ms，10次Agent模型调用（不冒充所有直接模型请求总数）。2行与前后稳定的独立MySQL SQL完全相同，result_correct=true、order_correct=true、limit=2，无错误。报告已保存artifacts/core-live-acceptance/http-core-smoke-report.json。

已核对容器内6个核心模块SHA256与工作区完全一致，记录artifacts/core-live-acceptance/deployment.json。不是仅重启旧镜像。未重建Web、Planner、Ontology、Executor、Postgres；仍6个常驻服务。当前本机两项开关true，仓库默认仍false。

回退：将semora-api:rollback-core-20261105重新标记为semora-api:dev，按rollback.json恢复.env两项开关（原不存在则移除，默认false），再执行scripts/update-mvp.ps1 -Services api。模型和超时未改，无业务数据迁移需回退。旧失败/澄清验收会话保留作证据，未清空用户历史。

本批核心查询、续聊修正、跨源、重复查询及部署后HTTP链路均有实际通过证据。未覆盖逐行AI文本评价、所有复杂预聚合形态、所有数据源方言及长期并发/性能分布；不把这些未验证能力称为完成。本次无新增UI、索引、缓存或常驻平台组件。
