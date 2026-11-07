import type { RunEvent } from './api'

export interface AnalysisOutcome {
  key: string; title: string; summary: string; details: string[]
  changes?: { path: string; before: unknown; after: unknown }[]
}
const text = (value: unknown) => typeof value === 'string' ? value : ''
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '未设置'
  return typeof value === 'string' ? value : JSON.stringify(value)
}

// Project immutable events into user-visible outputs; never count model calls as findings.
export function analysisOutcomes(events: RunEvent[]): AnalysisOutcome[] {
  const rows: AnalysisOutcome[] = []
  const seen = new Set<string>()
  const goals = new Map<string, string>()
  const add = (event: RunEvent, title: string, summary: string, details: string[] = [], changes?: AnalysisOutcome['changes']) => {
    if (!summary) return
    const signature = JSON.stringify([title, summary, details, changes])
    // Keep successive plan/review revisions in order, but collapse identical lookup results.
    if (event.event === 'agent.tool.completed' && seen.has(signature)) return
    seen.add(signature)
    rows.push({ key: `${event.created_at}-${rows.length}`, title, summary, details, changes })
  }
  for (const e of events) {
    const d = e.data || {}, o = d.observation || {}
    if (e.event === 'agent.tool.started') { goals.set(d.tool, text(d.analysis_goal)); continue }
    if (e.event === 'agent.tool.completed') {
      if (d.available === false) continue
      if (d.tool === 'search_wiki' && o.count > 0) {
        add(e, '找到相关知识', `检索“${o.keyword || ''}”，找到 ${o.count} 个相关页面`, (o.pages || []).map((p: any) => p.title || p.slug))
      } else if (d.tool === 'read_wiki' && o.title) {
        add(e, '读取知识依据', o.title, [text(o.excerpt), ...(o.source_refs || []).map((s: any) => typeof s === 'string' ? s : s.title || s.document_id || '')].filter(Boolean))
      } else if (d.tool === 'read_object' && o.object) {
        add(e, '取得对象定义', `${o.label || o.object}：${o.field_count} 个字段，${(o.relations || []).length} 条直接关系`, [o.object, ...(o.relations || [])])
      } else if (d.tool === 'find_relation_path' && o.path?.length) {
        add(e, '核对关联路径', o.path.join(' → '), [goals.get(d.tool) || '已找到配置中的关联路径'])
      } else if (d.tool === 'read_schema' && o.tables?.length) {
        add(e, '取得表结构', o.tables.map((t: any) => `${t.name}（${t.column_count} 个字段）`).join('、'))
      } else if (d.tool === 'read_source' && o.chunks?.length) {
        add(e, '取得原文证据', `已读取 ${o.chunks.length} 个原文片段`, o.chunks.map((c: any) => c.excerpt).filter(Boolean))
      }
      continue
    }
    if (e.event === 'plan.candidate.validated') {
      add(e, d.revision ? '修订查询方案' : '形成候选方案', text(d.summary),
        [...(d.conclusions || []), ...(d.additional_changes ? [`另有 ${d.additional_changes} 处变化，完整记录见审计。`] : [])], d.changes || [])
    } else if (e.event === 'plan.preflight.completed' || e.event === 'review.completed') {
      const review = d.review || d
      if (review.decision) add(e, review.decision === 'approved' ? '复核通过' : '复核发现问题', text(review.rationale),
        [...(review.findings?.length ? review.findings.map((f: any) => `${f.message}；修复要求：${f.acceptance}`) : review.issues || []),
         ...(review.resolutions || []).map((r: any) => `${r.status === 'resolved' ? '已解决' : '未解决'} ${r.finding_id}：${r.rationale}`)])
    } else if (e.event === 'alignment.required') {
      const req = d.requirement || {}
      const questions = (req.clarifications || []).map((q: any) => q.question)
      if (questions.length) add(e, '需要你补充', questions.join('；'), (req.clarifications || []).map((q: any) => q.reason).filter(Boolean))
    } else if (e.event === 'source.step.completed') {
      add(e, '完成数据查询', `${d.step || '查询步骤'}已完成${d.row_count !== undefined ? `，返回 ${d.row_count} 行` : ''}`)
    } else if (e.event === 'plan.revision.stalled') {
      add(e, '修订未取得进展', text(d.summary), d.finding_ids || [])
    }
  }
  return rows
}

export function activeAnalysisStatus(status: string, events: RunEvent[]): string {
  if (['completed', 'failed', 'clarifying', 'awaiting_approval'].includes(status)) return ''
  const stage = ({ compiling: '正在生成 SQL…', executing: '正在执行查询…', explaining: '正在整理结果说明…', retrieving: '正在检索相关知识…' } as Record<string, string>)[status]
  if (stage) return stage
  const latest = [...events].reverse().find(e => e.event === 'agent.tool.started' || e.event === 'agent.model.started')
  if (latest?.event === 'agent.tool.started' && latest.data.analysis_goal) return `正在核验：${latest.data.analysis_goal}`
  if (latest?.data.agent === 'reviewer' || status === 'reviewing') return '正在复核查询方案与需求是否一致…'
  return ({ compiling: '正在生成 SQL…', executing: '正在执行查询…', explaining: '正在整理结果说明…', retrieving: '正在检索相关知识…' } as Record<string, string>)[status] || '正在分析需求，形成下一项结论…'
}
