<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { HistoryIcon, ChevronRightIcon } from 'tdesign-icons-vue-next'
import type { QueryRun } from '../../api'
import { visibleQuestions, freeAnswerText } from '../../queryQuestions'
import { businessFailure, businessOutcomes, resultDescription, terminalStatus } from '../../queryPresentation'
import { displayValue } from '../../analysisTimeline'
import TaskStatus from './TaskStatus.vue'
import ExecutionRecord from './ExecutionRecord.vue'

const props = defineProps<{
  run: QueryRun; title: string; domainName: string; loading: boolean
  connection: string; lastSignal: number; answers: Record<string, string>
}>()
const emit = defineEmits<{
  'update:answers': [value: Record<string, string>]; confirm: []; retry: []; cancel: []
}>()
const recordOpen = ref(false)
watch(() => props.run.id, () => { recordOpen.value = false })
const questions = computed(() => visibleQuestions(props.run.requirement_draft?.clarifications || [], props.answers))
const gaps = computed(() => {
  const ids = new Set(questions.value.map(q => q.id))
  return (props.run.query_rehearsal?.technical_gaps || props.run.requirement_draft?.technical_gaps || [])
    .filter(g => (g.depends_on || []).every(c => ids.has(c.question_id) && c.values.includes(props.answers[c.question_id])))
})
const ready = computed(() => !gaps.value.length && questions.value.filter(q => q.blocking).every(q => !!props.answers[q.id]?.trim()))
const configurationOnly = computed(() => !questions.value.length && !!gaps.value.length)
const outcomes = computed(() => businessOutcomes(props.run.events || []))
const basis = computed(() => props.run.requirement?.summary || props.run.requirement_draft?.summary || '')
const labels = computed(() => props.run.plan?.sql_program?.steps?.at(-1)?.output_labels || {})
const explanation = computed(() => resultDescription(props.run))
const active = computed(() => !terminalStatus(props.run.status))
const currentStep = computed(() => {
  if (props.run.status === 'executing') {
    const started = [...props.run.events].reverse().find(e => e.event === 'source.step.started')
    const step = props.run.plan?.sql_program?.steps?.find((s: any) => s.name === started?.data.step)
    const completed = [...props.run.events].reverse().find(e => ['source.step.completed', 'source.step.skipped'].includes(e.event) && e.data.step === started?.data.step)
    if (step?.purpose && !completed) return step.purpose
  }
  return ({ received: '正在准备本次查询', retrieving: '正在查阅与需求相关的业务资料', understanding: '正在理解需求，核对所需业务信息', aligning: '正在整理需要确认的口径', aligned: '正在将已确认口径落实到查询', planning: '正在准备查询', compiling: '正在检查查询是否可以执行', explaining: '正在整理数据与结果说明' } as Record<string, string>)[props.run.status] || '正在获取本次查询所需信息'
})
function answer(id: string, value: string) {
  const next = { ...props.answers, [id]: value }
  const visible = new Set(visibleQuestions(props.run.requirement_draft?.clarifications || [], next).map(q => q.id))
  for (const key of Object.keys(next)) if (!visible.has(key)) delete next[key]
  emit('update:answers', next)
}
function openRecord() { recordOpen.value = true }
</script>

<template>
  <div class="task-detail">
    <header class="task-header">
      <div class="task-heading"><div class="task-eyebrow">{{ domainName }}<span>/</span>查询任务</div><h1>{{ title || '本次查询' }}</h1></div>
      <div class="task-header-actions"><button class="record-button" aria-label="查看本次查询的执行记录" :aria-expanded="recordOpen" @click="openRecord"><HistoryIcon/><span>执行记录</span></button><t-button v-if="active" size="small" variant="text" @click="emit('cancel')">取消查询</t-button></div>
    </header>
    <div class="task-content">
      <TaskStatus :run="run" :connection="connection" :last-signal="lastSignal"/>
      <section class="request-section"><div class="section-eyebrow">本次需求</div><p class="request-copy">{{ run.question }}</p></section>
      <section v-if="basis" class="business-basis"><h2>{{ run.requirement ? '本次统计口径' : run.status === 'clarifying' ? '当前理解' : '本次查询范围' }}</h2><p>{{ basis }}</p></section>

      <section v-if="run.status === 'clarifying'" class="clarification-section">
        <div class="section-heading"><h2>{{ configurationOnly ? '本次查询需要补齐配置' : '请确认以下业务口径' }}</h2><span v-if="questions.length">{{ questions.length }} 项待确认</span></div>
        <p class="section-description">{{ configurationOnly ? '所需关联或映射暂不完整，已有需求与回答会保留。' : '这些选择会影响查询结果，请一并确认。' }}</p>
        <div v-for="(q, index) in questions" :key="q.id" class="business-question" :data-question-id="q.id">
          <h3><span>{{ String(index + 1).padStart(2, '0') }}</span>{{ q.question }}</h3><p>{{ q.reason }}</p>
          <div v-if="q.options.length" class="business-options"><button v-for="o in q.options" :key="o.id" :disabled="loading" :aria-pressed="answers[q.id] === o.value" @click="answer(q.id, o.value)"><span class="choice-marker"></span><div><strong>{{ o.label }}</strong><small>{{ o.description }}</small></div></button></div>
          <textarea v-if="q.allow_free_text" :value="freeAnswerText(q, answers[q.id])" @input="answer(q.id, ($event.target as HTMLTextAreaElement).value)" :aria-label="`补充：${q.question}`" :disabled="loading" rows="2" :placeholder="q.options.length ? '选项不合适？直接描述您的想法也可以。' : '请补充您的想法。'"/>
        </div>
        <div v-if="gaps.length" class="configuration-notice"><strong>需要补齐的配置</strong><p v-for="(gap, index) in gaps" :key="index">{{ gap.summary }}</p><button class="inline-record" @click="openRecord">查看配置要求 <ChevronRightIcon/></button></div>
        <div class="clarification-footer"><p>{{ configurationOnly ? '配置更新后可继续检查当前需求。' : '确认后，按您的选择继续查询。' }}</p><t-button v-if="configurationOnly" theme="primary" :loading="loading" @click="emit('retry')">配置已更新，重新检查</t-button><t-button v-else theme="primary" :disabled="!ready" :loading="loading" @click="emit('confirm')">确认并继续查询</t-button></div>
      </section>

      <section v-else-if="active" class="running-section" aria-live="polite"><div class="running-mark"></div><div><h2>{{ currentStep }}</h2><p>您可以留在当前页面查看进度，也可以稍后从左侧重新打开这条查询。</p></div></section>

      <section v-else-if="run.status === 'failed'" class="failure-section"><div class="failure-symbol">!</div><div><h2>{{ run.delivery?.kind === 'cancelled' ? '本次查询已取消' : '本次查询未完成' }}</h2><p>{{ businessFailure(run) }}</p><div class="failure-actions"><t-button v-if="run.delivery?.kind !== 'cancelled'" theme="primary" :loading="loading" @click="emit('retry')">重试本次查询</t-button><button class="inline-record" @click="openRecord">查看执行记录 <ChevronRightIcon/></button></div></div></section>

      <section v-if="run.result" class="query-results">
        <div class="section-heading"><h2>{{ run.result.row_count ? `已返回 ${run.result.row_count.toLocaleString()} 条结果` : '没有符合条件的记录' }}</h2><span v-if="run.result.row_count > 100">预览前 100 条</span></div>
        <p v-if="explanation && run.status !== 'failed'" class="result-explanation">{{ explanation }}</p>
        <p v-if="run.result.truncated" class="result-limit">结果已达到本次返回上限，可能还有符合条件的记录。可在执行记录中查看查询限制。</p>
        <div v-if="run.result.row_count" class="result-table"><table><thead><tr><th v-for="(column, index) in run.result.columns" :key="column">{{ labels[column] || `结果 ${index + 1}` }}</th></tr></thead><tbody><tr v-for="(row, index) in run.result.rows.slice(0, 100)" :key="index"><td v-for="(cell, ci) in row" :key="ci">{{ displayValue(cell) }}</td></tr></tbody></table></div>
        <p v-else class="empty-result">查询已完成，当前条件下没有匹配数据。</p>
      </section>
      <section v-else-if="run.status === 'completed'" class="sql-only"><h2>本次查询语句已生成</h2><p>这是一条历史查询记录，未保存执行结果。可在执行记录中查看查询语句及其校验状态。</p><button class="inline-record" @click="openRecord">查看查询语句 <ChevronRightIcon/></button></section>
      <button v-if="run.delivery?.warnings?.length" class="result-warning" @click="openRecord">本次有 {{ run.delivery.warnings.length }} 项需留意的事项 <ChevronRightIcon/></button>

      <section v-if="outcomes.length" class="business-process"><div class="section-heading"><h2>本次处理</h2><button class="inline-record" @click="openRecord">完整执行记录 <ChevronRightIcon/></button></div><ol><li v-for="item in outcomes" :key="item.key"><span class="milestone-check">✓</span><div><strong>{{ item.title }}</strong><p>{{ item.summary }}</p></div></li></ol></section>
    </div>
    <ExecutionRecord v-model:visible="recordOpen" :run="run" :gaps="gaps"/>
  </div>
</template>

<style scoped>
.task-detail{height:100%;display:flex;flex-direction:column;background:var(--semora-card);min-width:0}.task-header{padding:25px 34px 22px;border-bottom:1px solid var(--semora-line);display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-shrink:0}.task-heading{min-width:0}.task-eyebrow{font-size:11px;color:var(--semora-muted);display:flex;gap:9px;align-items:center;letter-spacing:.3px}.task-eyebrow span{opacity:.5}.task-heading h1{font-size:19px;font-weight:600;line-height:1.5;margin:7px 0 0;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}.task-header-actions{display:flex;align-items:center;gap:8px;flex-shrink:0}.record-button{display:flex;align-items:center;gap:7px;border:1px solid var(--semora-line);background:var(--semora-card);color:var(--semora-ink);border-radius:22px;padding:9px 13px;cursor:pointer;font-size:12px;transition:background .15s}.record-button:hover{background:var(--semora-sidebar);border-color:var(--semora-green)}.record-button svg{font-size:17px}.task-content{flex:1;min-height:0;overflow:auto;padding:10px max(34px,calc((100% - 920px)/2)) 48px;scrollbar-width:thin}.request-section{padding:18px 0 25px}.section-eyebrow{font-size:11px;letter-spacing:.6px;color:var(--semora-muted)}.request-copy{font-size:17px;line-height:1.85;margin:9px 0 0;color:var(--semora-ink);white-space:pre-wrap}.business-basis{border-left:3px solid var(--semora-green);padding:1px 0 1px 16px;margin:0 0 32px}.business-basis h2{font-size:12px;color:var(--semora-muted);font-weight:500;margin:0 0 5px}.business-basis p{font-size:13px;line-height:1.9;color:var(--semora-ink);margin:0;white-space:pre-wrap}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:18px}.section-heading h2,.running-section h2,.failure-section h2,.sql-only h2{font-size:16px;font-weight:600;margin:0}.section-heading>span{font-size:12px;color:var(--semora-muted);white-space:nowrap}.section-description{font-size:13px;color:var(--semora-muted);margin:7px 0 2px;line-height:1.8}.clarification-section{border-top:1px solid var(--semora-line);padding-top:25px}.business-question{padding:23px 0;border-bottom:1px solid var(--semora-line)}.business-question h3{font-size:14px;line-height:1.8;font-weight:600;margin:0;display:flex;gap:10px}.business-question h3>span{font-size:11px;font-variant-numeric:tabular-nums;color:var(--semora-green-dark);margin-top:3px}.business-question>p{font-size:12px;line-height:1.9;color:var(--semora-muted);margin:7px 0 12px}.business-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.business-options button{display:flex;align-items:flex-start;gap:10px;text-align:left;border:1px solid var(--semora-line);background:var(--semora-card);border-radius:9px;padding:13px 14px;color:var(--semora-ink);cursor:pointer;transition:background .15s,border-color .15s}.business-options button:hover{border-color:var(--semora-green)}.business-options button[aria-pressed=true]{border-color:var(--semora-green);background:var(--semora-green-soft)}.choice-marker{width:14px;height:14px;margin-top:4px;border:1px solid var(--semora-line);border-radius:50%;flex-shrink:0}.business-options button[aria-pressed=true] .choice-marker{border:4px solid var(--semora-green);background:var(--semora-card)}.business-options strong{display:block;font-size:13px;font-weight:500;line-height:1.8}.business-options small{display:block;font-size:12px;line-height:1.7;color:var(--semora-muted);margin-top:3px}.business-options button:disabled{opacity:.65;cursor:default}.business-question textarea{width:100%;resize:vertical;min-height:64px;border:1px solid var(--semora-line);background:var(--semora-card);border-radius:8px;padding:11px 12px;margin-top:11px;font-size:13px;line-height:1.8;color:var(--semora-ink);font-family:inherit}.business-question textarea:focus{outline:2px solid var(--semora-green-soft);border-color:var(--semora-green)}.clarification-footer{display:flex;align-items:center;justify-content:space-between;gap:20px;padding-top:19px}.clarification-footer p{font-size:12px;color:var(--semora-muted);margin:0}.configuration-notice{padding:17px 0;font-size:13px;line-height:1.8}.configuration-notice p{color:var(--semora-muted)}.inline-record{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;color:var(--semora-green-dark);padding:0;font-size:12px;cursor:pointer;white-space:nowrap}.running-section{display:flex;align-items:flex-start;gap:15px;border-top:1px solid var(--semora-line);padding:28px 0}.running-section p,.sql-only p{font-size:13px;line-height:1.9;color:var(--semora-muted);margin:8px 0 0}.running-mark{width:10px;height:10px;border-radius:50%;background:var(--semora-green);box-shadow:0 0 0 5px var(--semora-green-soft);flex-shrink:0;margin-top:7px}.failure-section{display:flex;gap:14px;padding:24px 0;border-top:1px solid var(--semora-line)}.failure-symbol{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:color-mix(in srgb,#e34d59 10%,var(--semora-card));color:#c24951;font-weight:600;flex-shrink:0}.failure-section p{font-size:13px;line-height:1.9;color:var(--semora-muted);margin:9px 0 17px}.failure-actions{display:flex;gap:20px;align-items:center}.query-results{padding-top:5px}.result-explanation{font-size:13px;line-height:1.9;color:var(--semora-muted);margin:10px 0 18px}.result-table{overflow:auto;margin-top:17px;max-height:520px;border:1px solid var(--semora-line);border-radius:9px}.result-table table{border-collapse:collapse;width:100%;font-size:13px}.result-table th{position:sticky;top:0;background:var(--semora-sidebar);color:var(--semora-muted);font-weight:500;z-index:1}.result-table th,.result-table td{padding:13px 15px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--semora-line);font-variant-numeric:tabular-nums}.result-table tbody tr:last-child td{border-bottom:0}.result-table tbody tr:hover{background:var(--semora-sidebar)}.empty-result{padding:30px 0;color:var(--semora-muted);font-size:13px}.result-limit,.result-warning{font-size:12px;line-height:1.8;color:#9c691d;background:transparent}.result-warning{display:flex;align-items:center;gap:6px;border:0;padding:14px 0;cursor:pointer}.business-process{margin-top:30px;padding-top:24px;border-top:1px solid var(--semora-line)}.business-process .section-heading h2{font-size:14px}.business-process ol{list-style:none;padding:0;margin:13px 0 0}.business-process li{display:flex;gap:12px;padding:10px 0}.milestone-check{color:var(--semora-green);font-size:14px;margin-top:1px}.business-process strong{font-size:12px;font-weight:500}.business-process p{font-size:12px;line-height:1.8;color:var(--semora-muted);margin:3px 0 0}.sql-only{border-top:1px solid var(--semora-line);padding-top:25px}.sql-only .inline-record{margin-top:12px}@media(max-width:1000px){.task-header{padding:22px}.task-content{padding:8px 24px 36px}.business-options{grid-template-columns:1fr}.record-button span{display:none}.clarification-footer{align-items:flex-start;flex-direction:column}.task-header-actions{gap:3px}}
</style>
