<script setup lang="ts">
import { analysisOutcomes, activeAnalysisStatus, displayValue } from '../analysisTimeline'
import { computed, onMounted, reactive, ref } from 'vue'
import { api, type QueryRun, type QuerySession, type SystemSettings } from '../api'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import { refreshToken } from '../auth'
import { SearchIcon, SendIcon, AddIcon, CheckCircleIcon, ErrorCircleIcon, TimeIcon, ChevronDownIcon } from 'tdesign-icons-vue-next'

const sessions = ref<QuerySession[]>([])
const domains = ref<any[]>([])
const activeSession = ref('demo-session')
const selectedDomain = ref('retail_credit')
const requirementForm = reactive({ title: '', objective: '', time_range: '', scope: '', output: '' })
const modelConfigured = ref(false)
const pageError = ref('')
const sessionSearch = ref('')
const question = ref('')
const messageMode = ref<'auto' | 'new'>('auto')
const run = ref<QueryRun | null>(null)
const alignmentCard = ref<HTMLElement | null>(null)
const loading = ref(false)
let activeStream: AbortController | null = null
let selectionVersion = 0
const settings = ref<SystemSettings | null>(null)
const alignmentAnswers = ref<Record<string, string>>({})
const expanded = ref<Record<string, boolean>>({ agent: false, research: false, history: false, timeline: false, plan: false, sql: true, gate: false, evidence: false, sqlHistory: false })
const sourceSteps = computed(() => run.value?.plan?.source_steps || run.value?.requirement_draft?.source_steps || [])
function sourceStatus(name: string) {
  const event = [...(run.value?.events || [])].reverse().find(e => e.data.step === name && e.event.startsWith('source.step.'))
  return ({ 'source.step.gated': '门禁通过', 'source.step.started': '查询中', 'source.step.completed': '完成', 'source.step.skipped': '跳过：关联键为空' } as Record<string, string>)[event?.event || ''] || '等待执行'
}
const examples = ['最近12个月各分行贷款余额和90+逾期率', '查询贷款客户明细和手机号', '客户数']
const statusLabel: Record<string, string> = {
  received: '已接收', retrieving: '检索知识', understanding: '理解需求', aligning: '准备对齐', clarifying: '待确认口径', aligned: '口径已确认', planning: '生成查询计划', compiling: '生成 SQL', gating: '安全门禁', reviewing: '独立复核', awaiting_approval: '等待审批', executing: '执行查询', explaining: '解释结果', completed: '已完成', failed: '失败',
}
const progress = computed(() => (run.value?.events || []).filter((event, index, all) => index === 0 || event.status !== all[index - 1].status).slice(-8))
const approvedSql = computed(() => run.value?.gate?.allowed && (run.value?.review?.decision === 'approved' || run.value?.review?.disposition === 'advisory') ? run.value.sql : null)
const sqlHistory = computed(() => {
  const events = run.value?.events || []
  return events.flatMap((event, index) => {
    if (event.event !== 'sql.ready' || !event.data.sql || event.data.sql.artifact_hash === approvedSql.value?.artifact_hash) return []
    const following = events.slice(index + 1)
    const boundary = following.findIndex(e => e.event === 'sql.ready')
    const segment = boundary < 0 ? following : following.slice(0, boundary)
    const review = segment.find(e => e.event === 'review.completed')?.data.review
    return [{ sql: event.data.sql, review, created_at: event.created_at }]
  })
})
const lastRevision = computed(() => [...(run.value?.events || [])].reverse().find(e => e.event === 'review.revision.required'))
const nextAction = computed(() => {
  if (!run.value) return ''
  if (run.value.status === 'clarifying') {
    const count = run.value.requirement_draft?.clarifications.length || 0
    if (count) return `请回答下面 ${count} 项业务问题，提交后继续分析。`
    if (lastRevision.value) return 'SQL 经复核退回后已修订，请确认下面的新方案，再重新生成并复核 SQL。'
    return '业务口径已整理，请确认下面的方案后生成 SQL。'
  }
  if (run.value.review?.decision === 'revise') return 'Reviewer 要求修改 SQL，Analyst 正在修订。旧 SQL 保留在历史版本中，不会执行。'
  return statusLabel[run.value.status] || run.value.status
})
const agentTrace = computed(() => (run.value?.events || []).filter(e => e.event.startsWith('agent.') || e.event.startsWith('plan.')))
const analysisResults = computed(() => analysisOutcomes(run.value?.events || []))
const analysisStatus = computed(() => run.value ? activeAnalysisStatus(run.value.status, run.value.events || []) : '')
const approvalLabel = computed(() => ({ disabled: '审批已关闭', all: '全部审批', risk_based: '风险审批' }[settings.value?.approval_mode || 'risk_based']))
const activeSessionInfo = computed(() => sessions.value.find(item => item.id === activeSession.value))
const filteredSessions = computed(() => {
  const value = sessionSearch.value.trim().toLowerCase()
  if (!value) return sessions.value
  return sessions.value.filter(item => `${item.title} ${item.domain}`.toLowerCase().includes(value))
})
const activeDomain = computed(() => run.value?.domain || activeSessionInfo.value?.domain || selectedDomain.value)
const activeDomainName = computed(() => domains.value.find(item => item.domain === activeDomain.value)?.name || activeDomain.value)
const querySteps = computed(() => {
  const status = run.value?.status
  const reached = (statuses: string[]) => !!status && statuses.includes(status)
  return [
    { label: '提出问题', done: !!run.value, active: reached(['received', 'retrieving']) },
    { label: '理解需求', done: !!run.value?.requirement_draft, active: reached(['understanding', 'aligning']) },
    { label: '确认业务口径', done: !!run.value?.requirement, active: status === 'clarifying' },
    { label: '生成并审核 SQL', done: !!run.value?.sql && run.value?.review?.decision === 'approved', active: reached(['aligned', 'planning', 'compiling', 'gating', 'reviewing', 'awaiting_approval']) },
    { label: '查询结果', done: status === 'completed', active: reached(['executing', 'explaining', 'completed']) },
  ]
})
const alignmentReady = computed(() => {
  const questions = run.value?.requirement_draft?.clarifications || []
  return questions.filter(item => item.blocking).every(item => !!alignmentAnswers.value[item.id]?.trim())
})

function resetAlignmentAnswers() {
  alignmentAnswers.value = {}
}
function collapseHistoryPanels() {
  for (const key of ['agent', 'research', 'history', 'timeline', 'sqlHistory']) expanded.value[key] = false
}

async function loadSessions() {
  const version = selectionVersion
  sessions.value = (await api.get<QuerySession[]>('/sessions')).data
  if (version !== selectionVersion) return
  if (!sessions.value.some(item => item.id === activeSession.value) && sessions.value.length) {
    activeSession.value = sessions.value[0].id
  }
  const session = sessions.value.find(item => item.id === activeSession.value)
  if (session) selectedDomain.value = session.domain
}
async function createSession() {
  collapseHistoryPanels()
  selectionVersion += 1
  activeStream?.abort()
  loading.value = false
  activeSession.value = ''
  run.value = null
  messageMode.value = 'auto'
  question.value = ''
  pageError.value = ''
  Object.assign(requirementForm, { title: '', objective: '', time_range: '', scope: '', output: '' })
}
async function submitRequirement() {
  if (!requirementForm.objective.trim() || !modelConfigured.value) return
  pageError.value = ''
  try {
    const body = { title: requirementForm.title.trim() || requirementForm.objective.slice(0, 30), domain: selectedDomain.value, requirement_form: { ...requirementForm } }
    const current = sessions.value.find(item => item.id === activeSession.value)
    if (current && !current.run_count && current.domain === selectedDomain.value) {
      await api.put(`/sessions/${current.id}/requirement`, body)
    } else {
      activeSession.value = (await api.post('/sessions', body)).data.id
    }
    await loadSessions()
    await ask(requirementForm.objective)
  } catch (error: any) { pageError.value = error?.response?.data?.detail || '需求提交失败' }
}
async function selectSession(session: QuerySession) {
  messageMode.value = 'auto'
  collapseHistoryPanels()
  const version = ++selectionVersion
  activeStream?.abort()
  activeSession.value = session.id
  selectedDomain.value = session.domain
  run.value = null
  const { data } = await api.get<QueryRun[]>(`/sessions/${session.id}/runs`, { params: { limit: 1 } })
  if (version !== selectionVersion) return
  run.value = data[0] || null
  Object.assign(requirementForm, { title: '', objective: '', time_range: '', scope: '', output: '' }, session.requirement_form || {})
  resetAlignmentAnswers()
  if (run.value && !['clarifying', 'completed', 'failed', 'awaiting_approval'].includes(run.value.status)) {
    const id = run.value.id
    loading.value = true
    try { await streamRun(id, run.value.events.length) }
    catch { if (run.value?.id === id) pageError.value = '实时连接中断，可重新打开该会话查看进度' }
    finally { if (run.value?.id === id) loading.value = false }
  }
}
async function ask(text?: string) {
  const value = (text ?? question.value).trim()
  if (!value || loading.value) return
  if (run.value?.status === 'clarifying') {
    for (const item of run.value.requirement_draft?.clarifications || []) {
      if (!alignmentAnswers.value[item.id]?.trim()) alignmentAnswers.value[item.id] = value
    }
    question.value = ''
    await confirmAlignment()
    return
  }
  question.value = ''
  loading.value = true
  try {
    run.value = (await api.post(`/sessions/${activeSession.value}/messages`, { question: value, mode: messageMode.value })).data
    messageMode.value = 'auto'
    resetAlignmentAnswers()
    await streamRun(run.value!.id, run.value!.events.length)
    await loadSessions()
  } catch (error: any) { pageError.value = error?.response?.data?.detail || '分析请求失败' }
  finally { loading.value = false }
}
async function confirmAlignment() {
  if (!run.value || !alignmentReady.value || loading.value) return
  loading.value = true
  try {
    run.value = (await api.post<QueryRun>(`/runs/${run.value.id}/alignment`, { answers: alignmentAnswers.value, draft_version: run.value.requirement_draft?.version })).data
    resetAlignmentAnswers()
    if (!['clarifying', 'completed', 'failed', 'awaiting_approval'].includes(run.value.status)) {
      await streamRun(run.value.id, run.value.events.length)
    }
    await loadSessions()
  } catch (error: any) {
    pageError.value = error?.response?.data?.detail || '补充分析连接中断，请查看任务最新状态'
    if (run.value) {
      try { run.value = (await api.get<QueryRun>(`/runs/${run.value.id}`)).data } catch { /* Keep the last known run. */ }
    }
  }
  finally { loading.value = false }
}
async function streamRun(runId: string, after: number) {
  activeStream?.abort()
  const controller = new AbortController()
  activeStream = controller
  const token = await refreshToken()
  let refreshChain = Promise.resolve()
  const refreshSnapshot = () => {
    refreshChain = refreshChain.then(async () => {
      const latest = (await api.get<QueryRun>(`/runs/${runId}`)).data
      if (activeStream === controller && run.value?.id === runId && latest.events.length >= run.value.events.length) run.value = latest
    }).catch(() => { /* Final refresh or reconnect recovers a failed snapshot fetch. */ })
  }
  await fetchEventSource(`/api/v1/runs/${runId}/events?after=${after}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal: controller.signal,
    openWhenHidden: true,
    onmessage(message) {
      if (message.event === 'stream.closed') {
        controller.abort()
        return
      }
      if (!message.data || !run.value || run.value.id !== runId || activeStream !== controller) return
      const sequence = Number(message.id)
      if (sequence > 0 && sequence <= run.value.events.length) return
      const event = JSON.parse(message.data)
      run.value.events.push(event)
      run.value.status = event.status
      if (['evidence.collected', 'plan.ready', 'plan.repaired', 'plan.preflight.completed', 'sql.ready', 'gate.completed', 'review.completed', 'review.revision.required', 'analysis.revision.started', 'requirement.revised', 'alignment.required', 'alignment.resolved', 'source.plan.compiled', 'source.step.started', 'source.step.completed', 'source.step.skipped', 'source.step.gated', 'approval.required', 'execution.completed', 'result_review.completed', 'answer.completed', 'run.failed', 'authorization.denied', 'execution.cancelled'].includes(message.event)) {
        refreshSnapshot()
      }
    },
    onerror(error) { throw error },
  })
  await refreshChain
  if (activeStream === controller && run.value?.id === runId) run.value = (await api.get<QueryRun>(`/runs/${runId}`)).data
}
async function decide(approved: boolean) {
  if (!run.value?.approval) return
  loading.value = true
  try {
    run.value = (await api.post(`/approvals/${run.value.approval.id}/decision`, { approved, reason: approved ? '业务用途和机构范围已核对' : '用途说明不足' })).data
  } finally { loading.value = false }
}
async function cancelRun() {
  if (!run.value) return
  await api.post(`/executions/${run.value.id}/cancel`)
  run.value = (await api.get<QueryRun>(`/runs/${run.value.id}`)).data
  loading.value = false
}
function toggle(key: string) { expanded.value[key] = !expanded.value[key] }
function showAlignment() { alignmentCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
onMounted(async () => {
  const version = selectionVersion
  await Promise.all([
    loadSessions(),
    api.get('/domains').then(({ data }) => { domains.value = data }),
    api.get<SystemSettings>('/settings').then(({ data }) => { settings.value = data }),
    api.get('/model/status').then(({ data }) => { modelConfigured.value = data.configured }),
  ])
  if (version !== selectionVersion) return
  if (!domains.value.some(item => item.domain === selectedDomain.value) && domains.value.length) selectedDomain.value = domains.value[0].domain
  const current = sessions.value.find(item => item.id === activeSession.value)
  if (current) await selectSession(current)
})
</script>

<template>
  <div class="workbench">
    <aside class="session-list">
      <div class="session-head"><strong>查询会话</strong><button @click="createSession"><AddIcon /></button></div>
      <t-select v-model="selectedDomain" size="small" class="domain-picker" placeholder="选择新会话业务域">
        <t-option v-for="domain in domains" :key="domain.domain" :value="domain.domain" :label="`${domain.name} · ${domain.version}`" />
      </t-select>
      <div class="session-search"><SearchIcon /><input v-model="sessionSearch" placeholder="搜索会话" /></div>
      <div class="session-group">今天</div>
      <button v-for="session in filteredSessions" :key="session.id" class="session-item" :class="{ active: activeSession === session.id }" @click="selectSession(session)">
        <span>{{ session.title }}</span><small>{{ session.domain }} · {{ session.run_count }} 次查询</small>
      </button>
      <div class="session-tip"><strong>安全提示</strong><p>Semora 只执行通过本体、权限、Reviewer 与审批策略校验的只读 SQL。</p></div>
    </aside>

    <section class="query-space">
      <header class="query-header">
        <div><h2>{{ run?.question || '智能查数工作台' }}</h2><span><i class="status-dot"></i>{{ run ? `${statusLabel[run.status]} · ${run.domain}` : `业务域 ${activeDomainName}` }}</span></div>
        <div class="header-actions">
          <t-tag v-if="run?.ontology" theme="default" variant="light">运行本体 {{ run.ontology.version }}</t-tag>
          <t-button v-if="run && !['completed','failed','clarifying','awaiting_approval'].includes(run.status)" size="small" variant="outline" @click="cancelRun">取消查询</t-button>
          <t-tag theme="success" variant="light">{{ approvalLabel }} · 系统策略</t-tag>
        </div>
      </header>

      <div class="query-scroll">
        <div v-if="!run" class="welcome">
          <div class="welcome-mark"><span></span><span></span><span></span></div>
          <h1>先描述这次查数需求</h1>
          <p>{{ settings?.delivery_mode === 'best_effort' ? '先按当前理解查询并交付结果，假设和复核建议会明确列出；你可以继续对话调整。核心信息不足时再请你补充。' : '有歧义时，AI 会通过卡片请你补充；口径明确后，Reviewer 复核通过即自动查询，展示 SQL、结果和说明。' }}</p>
          <t-alert v-if="!modelConfigured" theme="warning" title="请先配置大模型"><template #message>需求分析需要真实模型连接。<router-link to="/settings#model">前往模型配置</router-link></template></t-alert>
          <form class="requirement-form" @submit.prevent="submitRequirement">
            <label>需求名称（选填）<t-input v-model="requirementForm.title" :maxlength="100" placeholder="例如：本月贷款情况分析" /></label>
            <label>您想查什么？<textarea v-model="requirementForm.objective" required maxlength="1500" rows="4" placeholder="描述业务目标、想了解的问题；不需要知道表名或写 SQL。" /></label>
            <div class="form-row"><label>时间范围（选填）<t-input v-model="requirementForm.time_range" :maxlength="150" placeholder="例如：今年、截至某日，或暂不确定" /></label><label>数据范围（选填）<t-input v-model="requirementForm.scope" :maxlength="200" placeholder="例如：某机构、某类客户" /></label></div>
            <label>希望怎样展示（选填）<t-input v-model="requirementForm.output" :maxlength="200" placeholder="例如：按机构汇总、查看明细、趋势对比" /></label>
            <t-button type="submit" theme="primary" :loading="loading" :disabled="!modelConfigured || !requirementForm.objective.trim()">提交需求，开始分析</t-button>
          </form>
        </div>

        <template v-else>
          <div class="query-steps" aria-label="查询进度">
            <div v-for="(step, index) in querySteps" :key="step.label" class="query-step" :class="{ active: step.active, done: step.done }">
              <span>{{ index + 1 }}</span><strong>{{ step.label }}</strong>
            </div>
          </div>
          <div class="next-action" aria-live="polite">
            <div><strong>{{ run.delivery?.title || (run.error ? '查询未完成' : run.status === 'clarifying' ? '当前需要您确认' : '当前进度') }}</strong><p>{{ run.delivery?.next_action || nextAction }}</p><p v-if="run.error && !run.delivery" class="bad-text">{{ run.error }}</p><details v-if="run.error && run.delivery"><summary>查看技术详情</summary><p>{{ run.error }}</p></details></div>
            <t-button v-if="run.status === 'clarifying'" theme="primary" variant="outline" @click="showAlignment">查看待确认口径</t-button>
          </div>
          <details v-if="lastRevision" class="revision-reason"><summary>查看最近一次复核退回原因</summary><p>{{ lastRevision.data.summary }}</p></details>
          <div v-if="run.status === 'awaiting_approval'" class="approval-banner"><div><strong>该查询需要人工审批</strong><p>请核对当前 SQL、风险与业务范围后决定是否执行。</p></div><div><t-button theme="default" @click="decide(false)">拒绝</t-button><t-button theme="primary" :loading="loading" @click="decide(true)">批准并执行</t-button></div></div>
          <div class="user-question">{{ run.question }}</div>
          <t-alert v-if="run.continuation" theme="info" title="继续当前需求" style="margin-top:12px">
            <template #message>原需求：{{ run.continuation.question_history[0] }}。已接续补充口径与复核意见，本次将重新校验方案。</template>
          </t-alert>
          <div v-if="activeSessionInfo?.requirement_form?.objective" class="requirement-context"><strong>本次需求：{{ activeSessionInfo.title }}</strong><p>{{ activeSessionInfo.requirement_form.objective }}</p><small>{{ activeSessionInfo.requirement_form.time_range || '时间待对齐' }} · {{ activeSessionInfo.requirement_form.scope || '范围待对齐' }}</small></div>
          <div v-if="run.alignment_rounds?.length" class="artifact-card">
            <button class="artifact-title" :aria-expanded="expanded.history" @click="toggle('history')"><span>已补充的历史口径 <em>{{ run.alignment_rounds.length }} 轮</em></span><ChevronDownIcon :class="{ rotate: expanded.history }"/></button>
            <div v-if="expanded.history" class="artifact-body"><div v-for="round in run.alignment_rounds" :key="round.key" class="dialogue-round"><strong>您补充的口径</strong><p v-for="answer in round.answers" :key="answer.field + answer.value">{{ answer.label }}</p><small>AI 更新理解：{{ round.draft.summary }}</small></div></div>
          </div>
          <t-alert v-for="(event, i) in run.events.filter(item => item.event === 'knowledge.warning')" :key="'knowledge-warning-'+i" theme="info" :message="event.data.message" />
          <div class="thinking-summary"><button class="trace-toggle" :aria-expanded="expanded.timeline" @click="toggle('timeline')">{{ expanded.timeline ? '收起' : '展开' }}阶段时间线</button><span>{{ run.events.length }} 个可审计事件 · Trace {{ run.id.slice(0, 8) }}</span></div>
          <div v-if="expanded.timeline" class="timeline">
            <div v-for="(item, index) in progress" :key="`${item.created_at}-${index}`" class="timeline-item" :class="item.status">
              <span class="timeline-node"><CheckCircleIcon v-if="!['failed','clarifying','awaiting_approval'].includes(item.status)"/><ErrorCircleIcon v-else-if="item.status === 'failed'"/><TimeIcon v-else/></span>
              <div><strong>{{ statusLabel[item.status] }}</strong><small>{{ new Date(item.created_at).toLocaleTimeString() }}</small></div>
            </div>
          </div>

          <div v-if="run.requirement_draft && !run.requirement" ref="alignmentCard" class="alignment-card">
            <div class="alignment-head">
              <div><span class="eyebrow">系统对需求的理解</span><h3>{{ run.requirement_draft.summary }}</h3></div>
              <t-tag :theme="run.requirement_draft.clarifications.length ? 'warning' : 'success'" variant="light">
                {{ run.requirement_draft.clarifications.length ? `${run.requirement_draft.clarifications.length} 项待确认` : '理解完整' }}
              </t-tag>
            </div>
            <div class="requirement-grid">
              <div><small>统计对象</small><strong>{{ run.requirement_draft.subject || '由已发布本体确定' }}</strong></div>
              <div><small>指标</small><strong>{{ run.requirement_draft.metrics.join('、') || '需要确认' }}</strong></div>
              <div><small>时间口径</small><strong>{{ run.requirement_draft.time_semantics || '未指定' }}</strong></div>
              <div><small>统计粒度</small><strong>{{ run.requirement_draft.grain || '由指标口径确定' }}</strong></div>
              <div><small>输出字段</small><strong>{{ run.requirement_draft.output_fields.join('、') || '按问题返回' }}</strong></div>
              <div><small>证据</small><strong>{{ run.evidence.length }} 条知识片段</strong></div>
            </div>
            <ul v-if="run.requirement_draft.analysis_steps?.length" class="analysis-steps"><li v-for="step in run.requirement_draft.analysis_steps" :key="step">{{ step }}</li></ul>
            <div v-for="item in run.requirement_draft.clarifications" :key="item.id" class="clarification-question">
              <strong>{{ item.question }}</strong><p>{{ item.reason }}</p>
              <div v-if="item.options.length" class="option-list">
                <button v-for="option in item.options" :key="option.id" :class="{ selected: alignmentAnswers[item.id] === option.value }" @click="alignmentAnswers[item.id] = option.value">
                  <span>{{ option.label }}</span><small>{{ option.description }}</small>
                </button>
              </div>
              <input v-if="item.allow_free_text" v-model="alignmentAnswers[item.id]" class="clarification-input" :placeholder="item.options.length ? '也可以输入其他业务口径' : '请输入业务口径'" />
            </div>
            <div v-if="run.requirement_draft.stages?.length" class="stage-plan">
              <strong>汇总方式 · 数据库执行一条 SQL</strong>
              <ol>
                <li v-for="stage in run.requirement_draft.stages" :key="stage.name">{{ stage.label || stage.source }}：先按 {{ stage.group_by.join('、') }} 汇总 {{ stage.measures.map(item => item.label || item.name).join('、') }}；{{ stage.join_type === 'inner' ? '只保留有匹配明细的记录' : '保留没有匹配明细的主记录' }}。</li>
                <li>关联预聚合结果后，最终按 {{ run.requirement_draft.dimensions.join('、') || '全部数据' }} 汇总，每组一行。</li>
              </ol>
            </div>
            <div class="alignment-actions">
              <p v-if="!run.requirement_draft.clarifications.length">{{ lastRevision ? '这是复核后的修订方案。确认后会重新生成并复核 SQL，历史 SQL 不会执行。' : '没有发现会改变结果的歧义。确认后才会生成 SQL。' }}</p>
              <p v-else>选一个选项或用自己的话补充。AI 会继续检查，仍有歧义则继续询问。</p>
              <t-button theme="primary" :disabled="!alignmentReady || run.status !== 'clarifying'" :loading="loading" @click="confirmAlignment">{{ run.requirement_draft.clarifications.length ? '提交补充，继续分析' : lastRevision ? '确认修订方案，重新生成 SQL' : '确认口径，生成 SQL' }}</t-button>
            </div>
          </div>

          <div v-if="run.requirement" class="aligned-summary">
            <span><CheckCircleIcon /> 已确认需求</span>
            <strong>{{ run.requirement.summary }}</strong>
            <small>{{ run.requirement.version }} · {{ run.requirement.confirmed_by }}</small>
          </div>

          <div v-if="sqlHistory.length" class="artifact-card">
            <button class="artifact-title" :aria-expanded="expanded.sqlHistory" @click="toggle('sqlHistory')"><span>历史 SQL 与草稿 <em>{{ sqlHistory.length }} 个版本 · 非最终交付</em></span><ChevronDownIcon :class="{ rotate: expanded.sqlHistory }"/></button>
            <div v-if="expanded.sqlHistory" class="artifact-body">
              <article v-for="(entry, index) in sqlHistory" :key="entry.created_at" class="sql-version">
                <strong>第 {{ index + 1 }} 版 · {{ entry.review?.decision === 'revise' ? '复核退回' : entry.review?.decision === 'rejected' ? '复核拒绝' : entry.review?.decision === 'approved' ? '历史版本' : '待完成复核' }}</strong>
                <p>{{ entry.review?.rationale || '草稿尚未形成最终交付，不能作为执行依据。' }}</p>
                <pre class="code-block">{{ entry.sql.physical_sql }}</pre>
              </article>
            </div>
          </div>

          <div v-if="analysisResults.length || analysisStatus || agentTrace.length" class="artifact-card">
            <button class="artifact-title" :aria-expanded="expanded.agent" aria-controls="agent-trace-panel" @click="toggle('agent')"><span>分析产出 <em>{{ analysisResults.length }} 项发现与结论</em></span><ChevronDownIcon :class="{ rotate: expanded.agent }"/></button>
            <p v-if="analysisStatus" class="analysis-live" role="status">{{ analysisStatus }}</p>
            <div v-if="expanded.agent" id="agent-trace-panel" class="artifact-body research-body">
              <p v-if="!analysisResults.length">尚未形成新的分析产出。</p>
              <div class="research-trace">
                <article v-for="(item, index) in analysisResults" :key="item.key">
                  <span>{{ index + 1 }}</span>
                  <div>
                    <strong>{{ item.title }}</strong><p>{{ item.summary }}</p>
                    <details v-if="item.details.length || item.changes?.length" class="agent-observation">
                      <summary>查看依据与变化</summary>
                      <ul v-if="item.details.length"><li v-for="(detail, i) in item.details" :key="i">{{ detail }}</li></ul>
                      <div v-for="change in item.changes" :key="change.path" class="analysis-change">
                        <strong>{{ change.path }}</strong>
                        <p>修改前：{{ displayValue(change.before) }}</p>
                        <p>修改后：{{ displayValue(change.after) }}</p>
                      </div>
                    </details>
                  </div>
                </article>
              </div>
              <details class="agent-observation technical-diagnostics">
                <summary>技术诊断（{{ agentTrace.length }} 条原始记录）</summary>
                <p>模型请求、耗时、重试与工具诊断，仅用于排查运行问题。</p>
                <div v-for="(event, index) in agentTrace" :key="index">
                  <small>{{ event.created_at }} · {{ event.event }}</small>
                  <p>{{ event.data.summary || event.event }}</p>
                  <details><summary>诊断字段</summary><pre>{{ JSON.stringify(event.data, null, 2) }}</pre></details>
                </div>
              </details>
            </div>
          </div>

          <div v-if="run.knowledge_research" class="artifact-card">
            <button class="artifact-title" @click="toggle('research')"><span><b>00</b> Wiki Agent 研究 <em>{{ run.knowledge_research.agent_id }} · {{ run.knowledge_research.evidence.length }} 条引用</em></span><ChevronDownIcon :class="{ rotate: expanded.research }"/></button>
            <div v-if="expanded.research" class="artifact-body research-body">
              <div v-if="run.knowledge_research.answer" class="research-answer"><small>WeKnora 研究结论</small><p>{{ run.knowledge_research.answer }}</p></div>
              <div class="research-trace">
                <article v-for="(step, index) in run.knowledge_research.steps" :key="`${step.kind}-${index}`">
                  <span>{{ index + 1 }}</span><div><strong>{{ step.tool || step.kind }}</strong><p>{{ step.summary }}</p></div>
                </article>
              </div>
              <small class="research-session">WeKnora Session：{{ run.knowledge_research.session_id }}</small>
            </div>
          </div>

          <div v-if="run.plan" class="artifact-card">
            <button class="artifact-title" @click="toggle('plan')"><span><b>01</b> 查询计划 <em>QueryPlan v{{ run.plan.version }}</em></span><ChevronDownIcon :class="{ rotate: expanded.plan }"/></button>
            <div v-if="expanded.plan" class="artifact-body plan-grid">
              <div><small>业务目标</small><strong>{{ run.plan.intent }}</strong></div><div><small>统计粒度</small><strong>{{ run.plan.grain }}</strong></div>
              <div><small>数据源绑定</small><strong>{{ run.plan.data_source_id }}</strong></div>
              <div><small>指标</small><div><t-tag v-for="metric in run.plan.metrics" :key="metric" theme="success" variant="light">{{ metric }}</t-tag></div></div>
              <div><small>本体关系路径</small><strong>{{ run.plan.relationship_path.join(' → ') }}</strong></div>
              <div class="wide" v-if="run.plan.stages?.length"><small>预聚合 → 关联 → 最终汇总 · 一条 SQL</small><ol><li v-for="stage in run.plan.stages" :key="stage.name">{{ stage.label || stage.name }}：{{ stage.source }} 按 {{ stage.group_by.join('、') }} 预聚合，{{ stage.join_type === 'inner' ? 'INNER JOIN' : 'LEFT JOIN' }}。</li></ol></div>
              <div class="wide"><small>明确假设</small><ul><li v-for="item in run.plan.assumptions" :key="item">{{ item }}</li></ul></div>
            </div>
          </div>

          <div v-if="run.evidence?.length" class="artifact-card">
            <button class="artifact-title" @click="toggle('evidence')"><span><b>02</b> 业务口径证据 <em>{{ run.evidence.length }} 条 · {{ run.knowledge_research?.provider || 'Knowledge Core' }}</em></span><ChevronDownIcon :class="{ rotate: expanded.evidence }"/></button>
            <div v-if="expanded.evidence" class="artifact-body evidence-list"><article v-for="item in run.evidence" :key="item.chunk_id"><strong>{{ item.title }}</strong><small>{{ item.locator }} · {{ item.revision }}</small><p>{{ item.summary }}</p></article></div>
          </div>

          <div v-if="sourceSteps.length" class="artifact-card source-plan">
            <button class="artifact-title" @click="toggle('sources')"><span><b>跨源</b> 分步查询计划 <em>{{ sourceSteps.length }} 个步骤 · 程序传递关联键</em></span><ChevronDownIcon :class="{ rotate: expanded.sources }"/></button>
            <div class="artifact-body">
              <ol class="source-step-list"><li v-for="step in sourceSteps" :key="step.name">
                <strong>{{ step.label || step.name }} · {{ sourceStatus(step.name) }}</strong>
                <p>{{ step.query.intent }} · 数据源 {{ run.source_artifacts?.[step.name]?.compilation?.data_source_label || step.data_source_id }}</p>
                <small v-for="binding in step.inputs" :key="binding.source_step">从 {{ binding.source_step }} 传递 {{ (binding.key_pairs?.length ? binding.key_pairs : [{ source_field: binding.source_field, target_field: binding.target_field }]).map((pair: any) => `${pair.source_field} → ${pair.target_field}`).join('；') }}；按完整键去重匹配，空键集合不匹配任何记录。</small>
                <details v-if="approvedSql && run.source_artifacts?.[step.name]" :open="expanded.sources">
                  <summary>查看本步 SQL{{ run.source_artifacts[step.name].compilation?.inputs_bound ? ' · 参数已绑定' : ' · 待前置步骤提供关联参数' }}</summary>
                  <pre class="code-block">{{ run.source_artifacts[step.name].physical_sql }}</pre>
                  <small>绑定参数 {{ Object.keys(run.source_artifacts[step.name].parameters).length }} 项，关联键由程序传递。</small>
                </details>
              </li></ol>
              <small>各数据源为独立只读事务，读取时间可能不同；中间结果不完整时停止，不会作为完整结果交付。</small>
            </div>
          </div>

          <div v-if="approvedSql && !sourceSteps.length" class="artifact-card">
            <button class="artifact-title" @click="toggle('sql')"><span><b>03</b> 当前 SQL <em>{{ run.review?.disposition === 'advisory' ? '带建议交付' : '复核通过' }} · {{ run.result ? '已执行' : run.status === 'executing' ? '执行中' : '尚未执行' }} · {{ approvedSql.dialect }} · {{ approvedSql.artifact_hash.slice(0, 10) }}</em></span><ChevronDownIcon :class="{ rotate: expanded.sql }"/></button>
            <div v-if="expanded.sql" class="artifact-body"><p v-if="run.sql_explanation">{{ run.sql_explanation }}</p><pre class="code-block">{{ approvedSql.physical_sql }}</pre><div class="parameters"><span>绑定参数</span><code>{{ approvedSql.parameters }}</code></div></div>
          </div>

          <div v-if="run.gate" class="artifact-card">
            <button class="artifact-title" @click="toggle('gate')"><span><b>04</b> 门禁与 Reviewer <em :class="approvedSql ? 'ok-text' : 'bad-text'">{{ run.review?.disposition === 'advisory' ? '保留复核意见，继续交付' : approvedSql ? '通过' : !run.gate.allowed ? '门禁阻断' : run.review?.decision === 'revise' ? '内部修订中' : run.review?.decision === 'rejected' ? '复核拒绝' : '等待独立复核' }} · {{ run.gate.risk_level }} risk</em></span><ChevronDownIcon :class="{ rotate: expanded.gate }"/></button>
            <div v-if="expanded.gate" class="artifact-body"><div class="check-grid"><div v-for="check in run.gate.checks" :key="check.name" :class="{ warn: !check.passed }"><CheckCircleIcon v-if="check.passed"/><TimeIcon v-else/><span><strong>{{ check.name }}</strong><small>{{ check.message }}</small></span></div></div><div v-if="run.review" class="review-box"><strong>Reviewer：{{ run.review.decision }}</strong><p>{{ run.review.rationale }}</p></div></div>
          </div>


            <div v-if="run.delivery && !run.result" class="artifact-card"><div class="artifact-body"><strong>{{ run.delivery.title }}</strong><p>{{ run.delivery.summary }}</p><p>{{ run.delivery.next_action }}</p><details v-if="run.sql && run.delivery.kind !== 'clarification'"><summary>查看已生成的 SQL（执行状态见当前任务）</summary><pre class="code-block">{{ run.sql.physical_sql }}</pre></details></div></div>
            <div v-if="run.delivery?.warnings?.length" class="artifact-card"><div class="artifact-body"><details open><summary>本次待核对 {{ run.delivery.warnings.length }} 项，可继续对话调整</summary><ul><li v-for="(warning, index) in run.delivery.warnings" :key="index">{{ warning }}</li></ul></details></div></div>
            <div v-if="run.result" class="result-section">
            <div class="result-head"><div><strong>查询结果</strong><span>{{ run.result.row_count }} 行 · {{ run.result.duration_ms }} ms</span></div><t-tag theme="success" variant="light">只读事务</t-tag></div>
            <div class="result-table"><table><thead><tr><th v-for="column in run.result.columns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, i) in run.result.rows" :key="i"><td v-for="(cell, j) in row" :key="j">{{ cell }}</td></tr></tbody></table></div>
            <div v-if="run.result_review" class="result-review"><strong>{{ run.result_review.disposition === 'advisory' ? '复核建议（结果已交付）' : '结果 Reviewer：' + run.result_review.decision }}</strong><span>{{ run.result_review.reviewer_mode }} · {{ run.result_review.rationale }}</span></div>
            <div class="answer"><span class="answer-icon">S</span><div><strong>Semora 说明</strong><p>{{ run.answer }}</p><small>答案基于本体 v{{ run.ontology?.version }}、{{ run.evidence.length }} 条证据和已审计 SQL。</small></div></div>
          </div>
        </template>
      </div>

      <div v-if="pageError" class="page-error">{{ pageError }}</div>
      <div v-if="run" class="composer">
        <textarea v-model="question" rows="2" :placeholder="run.status === 'failed' && messageMode !== 'new' ? '继续补充当前需求，已确认口径会保留…' : run.status === 'clarifying' ? '也可以在这里用自己的话补充当前需求…' : '围绕当前需求继续提问，或提出下一步查询…'" @keydown.enter.exact.prevent="ask()"></textarea>
        <div class="composer-foot"><span v-if="run.status !== 'failed'">{{ activeDomain }} · Reviewer 已开启</span><label v-else class="continuation-mode">本次消息 <select v-model="messageMode" aria-label="本次消息类型" :disabled="loading"><option value="auto">继续当前需求</option><option value="new">新需求</option></select></label><t-button shape="circle" theme="primary" :loading="loading" @click="ask()"><SendIcon/></t-button></div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.next-action{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:16px;border:1px solid var(--semora-line);border-left:4px solid var(--semora-green);border-radius:10px;margin-bottom:12px;background:var(--semora-card)}.next-action p{margin:6px 0 0;line-height:1.6;color:var(--semora-muted)}.trace-toggle{border:0;background:none;color:var(--semora-muted);cursor:pointer;padding:0}.revision-reason{margin:10px 0;font-size:13px;line-height:1.6}.revision-reason summary{cursor:pointer}.alignment-card{scroll-margin-top:16px}.sql-version+.sql-version{border-top:1px solid var(--semora-line);padding-top:14px;margin-top:14px}
.agent-observation{margin-top:8px}.agent-observation summary{cursor:pointer;color:var(--semora-green-dark)}.agent-observation pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto;font-size:12px;line-height:1.6;background:var(--semora-page);padding:10px;border-radius:6px}
.requirement-form{text-align:left;display:grid;gap:18px;margin:28px auto;max-width:680px;padding:24px;border:1px solid var(--semora-line);border-radius:14px}.requirement-form label{display:grid;gap:9px;font-size:13px}.requirement-form textarea{width:100%;padding:12px;border:1px solid var(--semora-line);border-radius:8px;background:var(--semora-card);color:inherit;font:inherit;resize:vertical}.form-row{display:grid;grid-template-columns:1fr 1fr;gap:16px}.welcome{margin-top:3vh!important}.requirement-context,.dialogue-round{margin:16px 0;padding:15px 18px;border-radius:10px;background:var(--semora-green-soft);font-size:13px}.requirement-context p,.dialogue-round p{line-height:1.6}.requirement-context small,.dialogue-round small{color:var(--semora-muted)}.analysis-steps{margin:14px 22px;padding-left:18px;color:var(--semora-muted);font-size:13px;line-height:1.8}.page-error{margin:10px 30px;padding:12px;border-radius:8px;color:#942c35;background:#fff0f1}@media(max-width:900px){.form-row{grid-template-columns:1fr}}
.workbench{display:flex;height:100%;background:var(--semora-card)}.session-list{width:270px;flex:none;background:var(--semora-sidebar);border-right:1px solid var(--semora-line);padding:22px 14px;display:flex;flex-direction:column}.session-head{display:flex;justify-content:space-between;align-items:center;padding:0 7px 12px;font-size:17px}.session-head button{border:0;background:var(--semora-green);color:white;width:30px;height:30px;border-radius:8px;cursor:pointer}.domain-picker{margin-bottom:10px}.session-search{height:38px;border:1px solid var(--semora-line);border-radius:8px;background:var(--semora-card);display:flex;align-items:center;gap:8px;padding:0 10px;color:var(--semora-muted)}.session-search input{border:0;outline:0;background:transparent;width:100%;color:inherit}.session-group{color:#a0a7a3;font-size:12px;margin:20px 8px 8px}.session-item{border:0;background:transparent;text-align:left;border-radius:8px;padding:12px;cursor:pointer;color:var(--semora-ink)}.session-item span,.session-item small{display:block}.session-item span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.session-item small{color:var(--semora-muted);margin-top:5px}.session-item.active{background:var(--semora-card);box-shadow:0 1px 5px rgba(0,0,0,.05)}.session-tip{margin-top:auto;border-top:1px solid var(--semora-line);padding:16px 8px 0}.session-tip p{color:var(--semora-muted);font-size:12px;line-height:1.6;margin-bottom:0}.query-space{flex:1;min-width:0;height:100%;display:flex;flex-direction:column}.query-header{height:78px;flex:none;border-bottom:1px solid var(--semora-line);display:flex;justify-content:space-between;align-items:center;padding:0 28px}.query-header h2{font-size:16px;margin:0 0 7px}.query-header span{font-size:12px;color:var(--semora-muted)}.query-scroll{flex:1;overflow:auto;padding:28px max(38px,calc((100% - 980px)/2));padding-bottom:150px}.welcome{max-width:820px;margin:9vh auto;text-align:center}.welcome-mark{width:64px;height:64px;margin:auto;position:relative;transform:rotate(30deg)}.welcome-mark span{position:absolute;width:38px;height:38px;border:5px solid var(--semora-green);border-radius:11px;left:5px;top:16px}.welcome-mark span:nth-child(2){left:20px;top:4px;opacity:.6}.welcome-mark span:nth-child(3){left:23px;top:27px;opacity:.25}.welcome h1{font-size:30px;margin:24px 0 12px}.welcome>p{color:var(--semora-muted);line-height:1.7}.example-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:35px;text-align:left}.example-grid button{background:var(--semora-card);border:1px solid var(--semora-line);border-radius:10px;padding:16px;cursor:pointer;color:inherit}.example-grid button:hover{border-color:var(--semora-green);box-shadow:0 5px 20px rgba(7,192,95,.08)}.example-grid span,.example-grid small{display:block}.example-grid small{color:var(--semora-muted);margin-top:10px}.user-question{margin-left:auto;background:#f1f3f2;border-radius:12px 12px 2px 12px;padding:13px 17px;width:max-content;max-width:75%;font-size:16px}[theme-mode="dark"] .user-question{background:#29332d}.thinking-summary{display:flex;justify-content:space-between;align-items:center;margin:28px 0 15px}.thinking-summary span{color:var(--semora-muted);font-size:12px}.timeline{display:flex;gap:0;border:1px solid var(--semora-line);border-radius:10px;padding:15px;margin-bottom:16px;overflow:auto}.timeline-item{min-width:120px;display:flex;gap:8px;align-items:center;position:relative}.timeline-item:after{content:"";height:1px;background:var(--semora-line);flex:1;margin:0 7px}.timeline-item:last-child:after{display:none}.timeline-node{color:var(--semora-green);font-size:19px}.timeline-item div strong,.timeline-item div small{display:block;white-space:nowrap}.timeline-item div strong{font-size:12px}.timeline-item div small{font-size:10px;color:var(--semora-muted);margin-top:3px}.artifact-card{border:1px solid var(--semora-line);border-radius:10px;margin:11px 0;overflow:hidden}.artifact-title{width:100%;border:0;background:var(--semora-card);color:inherit;display:flex;justify-content:space-between;align-items:center;padding:15px 17px;cursor:pointer}.artifact-title b{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:7px;background:var(--semora-green-soft);color:var(--semora-green-dark);font-size:11px;margin-right:9px}.artifact-title em{font-style:normal;font-weight:400;color:var(--semora-muted);margin-left:10px;font-size:12px}.artifact-title svg{transition:transform .2s}.artifact-title svg.rotate{transform:rotate(180deg)}.artifact-body{border-top:1px solid var(--semora-line);padding:16px 18px;background:color-mix(in srgb,var(--semora-card) 96%,var(--semora-page))}.plan-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.plan-grid small{display:block;color:var(--semora-muted);margin-bottom:6px}.plan-grid strong{font-size:13px}.plan-grid .wide{grid-column:1/-1}.plan-grid ul{margin:6px 0;padding-left:20px;line-height:1.8}.plan-grid .t-tag{margin-right:5px}.evidence-list{display:grid;grid-template-columns:1fr 1fr;gap:10px}.evidence-list article{border:1px solid var(--semora-line);border-radius:8px;padding:13px}.evidence-list small{display:block;color:var(--semora-muted);margin-top:5px}.evidence-list p{font-size:13px;line-height:1.6;margin-bottom:0}.parameters{font-size:12px;color:var(--semora-muted);margin-top:11px}.parameters code{margin-left:12px;color:var(--semora-ink)}.check-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.check-grid>div{display:flex;align-items:flex-start;gap:7px;color:var(--semora-green)}.check-grid>div.warn{color:#e6a23c}.check-grid strong,.check-grid small{display:block}.check-grid strong{font-size:12px;color:var(--semora-ink)}.check-grid small{font-size:11px;color:var(--semora-muted);margin-top:3px}.review-box{border-left:3px solid var(--semora-green);padding:8px 12px;margin-top:16px;background:var(--semora-green-soft);color:#173b27}.review-box p{margin:5px 0 0;font-size:13px}.ok-text{color:var(--semora-green)!important}.bad-text{color:#e34d59!important}.notice,.approval-banner{border-radius:10px;padding:18px;margin:13px 0}.notice.warning{background:#fff8e7;border:1px solid #f6d78b;color:#765715}.notice.error{background:#fff0f1;border:1px solid #f3b9bd;color:#942c35}.notice p{margin-bottom:0}.quick-answers{display:flex;gap:8px;margin-top:12px}.quick-answers button{border:1px solid #d7ba73;background:white;border-radius:7px;padding:7px 11px;cursor:pointer}.approval-banner{border:1px solid #e8c77a;background:#fff9e9;display:flex;justify-content:space-between;align-items:center;color:#5e4815}.approval-banner p{margin:5px 0 0}.approval-banner .t-button{margin-left:8px}.result-section{border:1px solid var(--semora-line);border-radius:10px;margin-top:14px;overflow:hidden}.result-head{padding:16px 18px;display:flex;justify-content:space-between}.result-head strong,.result-head span{display:block}.result-head span{font-size:12px;color:var(--semora-muted);margin-top:5px}.result-table{overflow:auto;border-top:1px solid var(--semora-line);border-bottom:1px solid var(--semora-line)}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:12px 16px;border-bottom:1px solid var(--semora-line);white-space:nowrap}th{background:var(--semora-sidebar);color:var(--semora-muted)}.answer{display:flex;gap:12px;padding:18px}.answer-icon{width:34px;height:34px;display:grid;place-items:center;border-radius:9px;background:var(--semora-green);color:white;font-weight:700}.answer p{line-height:1.7;margin:7px 0}.answer small{color:var(--semora-muted)}.composer{position:absolute;left:calc(224px + 270px);right:0;bottom:0;padding:0 max(38px,calc((100% - 224px - 270px - 980px)/2)) 18px;background:linear-gradient(transparent,var(--semora-card) 25%)}.composer>textarea{width:100%;min-height:86px;resize:none;border:1px solid var(--semora-line);border-bottom:0;border-radius:13px 13px 0 0;padding:17px;outline:0;background:var(--semora-card);color:inherit;box-shadow:0 -5px 28px rgba(0,0,0,.04)}.composer>textarea:focus{border-color:var(--semora-green)}.composer-foot{height:48px;border:1px solid var(--semora-line);border-top:0;border-radius:0 0 13px 13px;padding:0 10px 8px 17px;display:flex;justify-content:space-between;align-items:center;background:var(--semora-card)}.composer-foot span{font-size:11px;color:var(--semora-muted)}
.header-actions{display:flex;align-items:center;gap:10px}
.result-review{display:flex;justify-content:space-between;gap:16px;padding:12px 18px;background:var(--semora-green-soft);color:#173b27;font-size:12px}.result-review span{color:var(--semora-muted)}
.query-steps{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:28px;padding:10px;border:1px solid var(--semora-line);border-radius:12px;background:var(--semora-sidebar)}
.query-step{display:flex;align-items:center;gap:8px;min-width:0;padding:8px 10px;border-radius:8px;color:var(--semora-muted)}
.query-step span{width:24px;height:24px;display:grid;place-items:center;flex:none;border-radius:50%;background:var(--semora-card);border:1px solid var(--semora-line);font-size:12px}.query-step strong{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.query-step.active{background:var(--semora-card);color:var(--semora-green-dark);box-shadow:0 1px 6px rgba(0,0,0,.05)}.query-step.active span{border-color:var(--semora-green);color:var(--semora-green)}.query-step.done span{background:var(--semora-green);border-color:var(--semora-green);color:white}
.alignment-card{margin:14px 0;border:1px solid color-mix(in srgb,var(--semora-green) 42%,var(--semora-line));border-radius:12px;overflow:hidden;background:var(--semora-card);box-shadow:0 8px 28px rgba(7,192,95,.07)}
.alignment-head{display:flex;justify-content:space-between;gap:20px;padding:20px 22px;border-bottom:1px solid var(--semora-line)}.alignment-head h3{margin:6px 0 0;font-size:17px;line-height:1.55}.eyebrow{font-size:11px;color:var(--semora-green-dark);font-weight:700;letter-spacing:.04em}
.requirement-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;padding:18px 22px;background:color-mix(in srgb,var(--semora-green-soft) 45%,var(--semora-card))}.requirement-grid small,.requirement-grid strong{display:block}.requirement-grid small{color:var(--semora-muted);margin-bottom:6px}.requirement-grid strong{font-size:13px;line-height:1.5}
.clarification-question{padding:19px 22px;border-top:1px solid var(--semora-line)}.clarification-question>p{margin:7px 0 13px;color:var(--semora-muted);font-size:13px}.option-list{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.option-list button{border:1px solid var(--semora-line);border-radius:9px;background:var(--semora-card);padding:11px 12px;text-align:left;color:inherit;cursor:pointer}.option-list button:hover,.option-list button.selected{border-color:var(--semora-green);background:var(--semora-green-soft)}.option-list span,.option-list small{display:block}.option-list small{margin-top:5px;color:var(--semora-muted);line-height:1.45}
.clarification-input{width:100%;margin-top:10px;padding:10px 12px;border:1px solid var(--semora-line);border-radius:8px;background:var(--semora-card);color:inherit;outline:0}.clarification-input:focus{border-color:var(--semora-green)}.alignment-actions{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:16px 22px;border-top:1px solid var(--semora-line)}.alignment-actions p{margin:0;color:var(--semora-muted);font-size:12px}
.aligned-summary{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:12px;margin:12px 0;padding:13px 16px;border:1px solid var(--semora-line);border-radius:10px;background:var(--semora-green-soft)}.aligned-summary span{display:flex;align-items:center;gap:6px;color:var(--semora-green-dark);font-size:12px}.aligned-summary strong{font-size:13px}.aligned-summary small{color:var(--semora-muted)}
.research-body{display:grid;gap:14px}.research-answer{border-left:3px solid var(--semora-green);padding:2px 12px}.research-answer small,.research-session{color:var(--semora-muted)}.research-answer p{margin:6px 0 0;line-height:1.7}.research-trace{display:grid;gap:8px}.research-trace article{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--semora-line);border-radius:8px;padding:10px 12px}.research-trace article>span{width:22px;height:22px;display:grid;place-items:center;flex:none;border-radius:50%;background:var(--semora-green-soft);color:var(--semora-green-dark);font-size:11px}.research-trace strong{font-size:12px}.research-trace p{margin:4px 0 0;color:var(--semora-muted);font-size:12px;line-height:1.55}
.stage-plan{padding:14px;border-radius:8px;background:var(--semora-page);margin:12px 0}.stage-plan ol{padding-left:22px;margin:8px 0 0;line-height:1.8;color:var(--semora-muted)}
@media (max-width:1100px){.query-steps{grid-template-columns:1fr}.query-step{display:none}.query-step.active{display:flex}.requirement-grid{grid-template-columns:1fr 1fr}.option-list{grid-template-columns:1fr}}
.source-step-list{padding-left:22px;margin:0}.source-step-list li{padding:8px 0 16px}.source-step-list p{font-size:13px;color:var(--semora-muted);margin:6px 0}.source-step-list small{display:block;line-height:1.7}.source-step-list details{margin-top:8px}.source-step-list summary{cursor:pointer;color:var(--semora-green-dark);font-size:13px}
.continuation-mode{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--semora-muted)}.continuation-mode select{border:1px solid var(--semora-line);border-radius:6px;padding:4px 8px;background:var(--semora-card);color:var(--semora-ink)}
</style>


<style scoped>
.analysis-live { margin: 0; padding: 12px 20px; color: var(--td-text-color-secondary); }
.analysis-change { margin: 10px 0; padding: 10px; background: var(--td-bg-color-container-hover); border-radius: 6px; overflow-wrap: anywhere; }
.technical-diagnostics { margin-top: 18px; border-top: 1px solid var(--td-component-border); padding-top: 12px; }
.technical-diagnostics > div { padding: 8px 0; }
</style>
