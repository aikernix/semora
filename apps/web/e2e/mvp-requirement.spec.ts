import { expect, test } from '@playwright/test'

for (const newRequest of [false, true]) {
  test(`failed conversation sends explicit mode and shows handoff: new=${newRequest}`, async ({ page }) => {
    const failed = { id: 'failed-run', session_id: 'continue-session', domain: 'commerce', question: '按地区统计金额', status: 'failed', error: '方案修订未完成', evidence: [], events: [] }
    let submitted: any
    const next = { ...failed, id: 'next-run', question: '只统计有效记录', status: 'clarifying', error: null,
      events: [{ event: 'agent.model.started', status: 'understanding', created_at: '2026-11-04T06:00:00Z', data: { agent: 'analyst', iteration: 1, phase: '失败后继续分析' } }],
      requirement_draft: { version: 2, summary: '按地区统计有效记录金额', metrics: [], dimensions: [], filters: [], assumptions: [], clarifications: [], analysis_steps: [], output_fields: [], aggregation_rules: [], stages: [] },
      continuation: newRequest ? null : { source_run_id: 'failed-run', root_run_id: 'failed-run', question_history: ['按地区统计金额'], model_round_offset: 8, role_rounds: { analyst: 6, reviewer: 2 } } }
    await page.route('**/api/v1/**', route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'continue-session', title: '金额分析', domain: 'commerce', run_count: 1 }] })
      if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'commerce', name: '业务分析' }] })
      if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
      if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
      if (path === '/api/v1/sessions/continue-session/runs') return route.fulfill({ json: [failed] })
      if (path === '/api/v1/sessions/continue-session/messages') { submitted = route.request().postDataJSON(); return route.fulfill({ status: 202, json: next }) }
      if (path === '/api/v1/runs/next-run/events') return route.fulfill({ contentType: 'text/event-stream', body: ['event: stream.closed', 'data: {}', '', ''].join(String.fromCharCode(10)) })
      if (path === '/api/v1/runs/next-run') return route.fulfill({ json: next })
      return route.fulfill({ json: {} })
    })
    await page.goto('/workbench')
    await expect(page.getByLabel('本次消息类型')).toHaveValue('auto')
    if (newRequest) await page.getByLabel('本次消息类型').selectOption('new')
    await page.locator('.composer textarea').fill('只统计有效记录')
    await page.locator('.composer button').click()
    await expect.poll(() => submitted?.mode).toBe(newRequest ? 'new' : 'auto')
    await expect(page.locator('.user-question')).toHaveText('只统计有效记录')
    await expect(page.getByText('原需求：按地区统计金额', { exact: false })).toHaveCount(newRequest ? 0 : 1)
    await page.getByRole('button', { name: '多轮分析与协作', exact: false }).click()
    await expect(page.getByText(`任务累计第 ${newRequest ? 1 : 9} 轮`, { exact: false })).toBeVisible()
  })
}

test('confirmation exposes preaggregation and row-preserving join semantics', async ({ page }) => {
  const stage = { name: 'lines_by_order', label: '订单明细汇总', source: 'line', group_by: ['line.order_id'], join_type: 'left', measures: [{ name: 'units', label: '商品数量', function: 'sum', field: 'line.quantity' }] }
  const draft = { version: 1, summary: '按地区汇总订单与商品数量', subject: '订单', metrics: ['amount', 'units'], dimensions: ['region.name'], filters: [], aggregation_rules: [], stages: [stage], output_fields: [], assumptions: [], analysis_steps: [], clarifications: [] }
  const run = { id: 'stage-run', session_id: 'stage-session', domain: 'commerce', question: draft.summary, status: 'clarifying', requirement_draft: draft, evidence: [], events: [] }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'stage-session', title: draft.summary, domain: 'commerce', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'commerce', name: '电商业务', version: '1' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
    if (path === '/api/v1/sessions/stage-session/runs') return route.fulfill({ json: [run] })
    return route.fulfill({ json: {} })
  })
  await page.goto('/workbench')
  await expect(page.getByText('汇总方式 · 数据库执行一条 SQL', { exact: true })).toBeVisible()
  await expect(page.getByText('保留没有匹配明细的主记录', { exact: false })).toBeVisible()
  await expect(page.getByText('最终按 region.name 汇总，每组一行', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '确认口径，生成 SQL', exact: true })).toBeVisible()
})

for (const bankVisible of [false, true]) {
  test(`knowledge binding never falls back to another account's first base: ${bankVisible}`, async ({ page }) => {
    const bankId = 'bank-kb'
    const bases = bankVisible ? [{ id: bankId, name: '银行数据分析知识库', documents: 12 }] : [{ id: 'hr-kb', name: 'HR知识库', documents: 10 }]
    const writes: string[] = []
    await page.route('**/api/v1/**', route => {
      const path = new URL(route.request().url()).pathname
      if (route.request().method() !== 'GET') writes.push(path)
      if (path === '/api/v1/knowledge/status') return route.fulfill({ json: { status: 'ready', provider: 'weknora', instance_url: 'http://example.test', api_key_configured: true } })
      if (path === '/api/v1/knowledge-bases') return route.fulfill({ json: bases })
      if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'bank', name: '业务域', version: '1' }] })
      if (path === '/api/v1/ontology/drafts/bank') return route.fulfill({ json: { domain: 'bank', knowledgeBaseIds: [bankId] } })
      return route.fulfill({ json: {} })
    })
    await page.goto('/knowledge')
    if (bankVisible) {
      await expect(page.locator('.kb-card')).toContainText('银行数据分析知识库')
      await expect(page.locator('.kb-card')).toContainText('当前已绑定')
    } else {
      await expect(page.getByText('当前连接无法访问已绑定知识库', { exact: true })).toBeVisible()
      await expect(page.getByText('系统不会自动选择其他知识库', { exact: false })).toBeVisible()
      await expect(page.locator('.kb-card')).toHaveCount(0)
    }
    await expect(page.getByRole('button', { name: '保存知识库绑定', exact: true })).toBeDisabled()
    expect(writes).toEqual([])
  })
}

test('ontology studio distinguishes the draft from the published query version', async ({ page }) => {
  const draft = { domain: 'example', name: '示例业务域', version: '0.2.0', objects: [], relations: [], metrics: [] }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontology/drafts') return route.fulfill({ json: [draft] })
    if (path === '/api/v1/ontology/drafts/example') return route.fulfill({ json: draft })
    if (path === '/api/v1/ontology/candidates') return route.fulfill({ json: [] })
    if (path === '/api/v1/ontology/publication-status') return route.fulfill({ json: { domain: 'example', draft_version: '0.2.0', published_version: '0.1.0', published_counts: { objects: 3, relations: 0, metrics: 0 } } })
    return route.fulfill({ json: {} })
  })
  await page.goto('/ontology')
  await expect(page.getByText('新查询使用已发布本体 0.1.0', { exact: true })).toBeVisible()
  await expect(page.getByText('保存草稿不会影响查询', { exact: false })).toBeVisible()
  await expect(page.getByText('当前编辑的是草稿 0.2.0', { exact: false })).toBeVisible()
})

for (const decision of [null, 'revise', 'rejected', 'approved']) {
  test(`only independently approved SQL is delivered: ${decision ?? 'pending'}`, async ({ page }) => {
    const sql = { physical_sql: 'SELECT COUNT(*) FROM loans', artifact_hash: 'draft-1', dialect: 'mysql', parameters: {} }
    const run = { id: 'delivery-run', session_id: 'delivery-session', domain: 'bank_analysis', question: '贷款数', status: decision === 'approved' ? 'completed' : 'failed', error: '测试记录', evidence: [], sql,
      gate: { allowed: true, risk_level: 'low', checks: [] }, review: decision ? { decision, rationale: '核验条件' } : null,
      events: [{ event: 'sql.ready', status: 'compiling', created_at: '2026-11-04T02:00:00Z', data: { sql } }] }
    await page.route('**/api/v1/**', route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'delivery-session', title: '贷款数', domain: 'bank_analysis', run_count: 1 }] })
      if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'bank_analysis', name: '银行数据分析' }] })
      if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
      if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
      if (path === '/api/v1/sessions/delivery-session/runs') return route.fulfill({ json: [run] })
      return route.fulfill({ json: run })
    })
    await page.goto('/workbench')
    const delivered = page.getByRole('button', { name: '当前 SQL', exact: false })
    if (decision === 'approved') {
      await expect(delivered).toBeVisible()
      await expect(page.getByText(sql.physical_sql, { exact: true })).toBeVisible()
    } else {
      await expect(page.getByRole('button', { name: '历史 SQL 与草稿', exact: false })).toBeVisible()
      await expect(delivered).toHaveCount(0)
      await expect(page.getByText(sql.physical_sql, { exact: true })).toHaveCount(0)
    }
  })
}

test('model settings expose editable connection fields without returning a saved key', async ({ page }) => {
  await page.route('**/api/v1/settings/model', route => route.fulfill({ json: { base_url: '', model: 'example-model', api_key_configured: false, configured: false } }))
  await page.goto('/settings#model')
  await expect(page.getByText('基础模型配置', { exact: false }).first()).toBeVisible()
  await expect(page.getByPlaceholder('https://your-provider.example/v1')).toBeVisible()
  await expect(page.getByPlaceholder('填写服务商提供的模型 ID')).toHaveValue('example-model')
  await expect(page.getByPlaceholder('输入 API Key')).toHaveAttribute('type', 'password')
  await expect(page.getByRole('button', { name: '测试连接', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '保存模型配置', exact: true })).toBeVisible()
})

test('a new chat starts with a requirement form and explains missing model configuration', async ({ page }) => {
  await page.route('**/api/v1/model/status', route => route.fulfill({ json: { configured: false } }))
  await page.goto('/workbench')
  await page.locator('.session-head button').click()
  await expect(page.getByRole('heading', { name: '先描述这次查数需求' })).toBeVisible()
  await expect(page.getByRole('link', { name: '前往模型配置' })).toBeVisible()
  await page.getByPlaceholder('描述业务目标、想了解的问题；不需要知道表名或写 SQL。').fill('统计今年各机构的贷款账户数')
  await expect(page.getByRole('button', { name: '提交需求，开始分析' })).toBeDisabled()
})

test('clarification cards accept options and free text then show the next question without SQL', async ({ page }) => {
  const question = { id: 'metric', field: 'metric', question: '您要统计贷款账户数还是客户数？', reason: '统计对象不同', allow_free_text: true, blocking: true, options: [
    { id: 'loan', label: '贷款账户数', value: '按贷款账户去重', description: '一笔贷款统计一次' },
    { id: 'customer', label: '贷款客户数', value: '按客户去重', description: '同一客户统计一次' },
  ] }
  const draft = { version: 1, summary: '查询贷款情况，统计对象待确认', subject: '贷款', metrics: [], dimensions: [], filters: [], grain: null, time_semantics: null, output_fields: [], assumptions: [], analysis_steps: ['统计对象会改变去重规则'], clarifications: [question], status: 'awaiting_clarification' }
  const run: any = { id: 'ui-run', session_id: 'ui-session', domain: 'bank_analysis', question: '查询贷款情况', status: 'clarifying', events: [], evidence: [], requirement_draft: draft }
  let submitted: any
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'ui-session', title: '本月贷款分析', domain: 'bank_analysis', run_count: 1, requirement_form: { objective: '查询贷款情况' } }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'bank_analysis', name: '银行数据分析', version: '0.2.0' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
    if (path === '/api/v1/sessions/ui-session/runs') return route.fulfill({ json: [run] })
    if (path === '/api/v1/runs/ui-run/alignment') {
      submitted = route.request().postDataJSON()
      return route.fulfill({ json: { ...run, requirement_draft: { ...draft, version: 2, summary: '统计口径已补充，继续确认时间', clarifications: [{ ...question, id: 'time', field: 'time', question: '查询哪个时间范围？', options: [{ id: 'all', label: '全部数据', value: '全部数据', description: '不限制日期' }, { id: 'year', label: '今年', value: '今年', description: '本年数据' }] }] } } })
    }
    return route.continue()
  })
  await page.goto('/workbench')
  await expect(page.getByText(question.question, { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '贷款账户数 一笔贷款统计一次' }).click()
  const freeText = page.getByPlaceholder('也可以输入其他业务口径')
  await expect(freeText).toHaveValue('按贷款账户去重')
  await freeText.fill('按贷款账户去重，并排除已结清账户')
  await page.getByRole('button', { name: '提交补充，继续分析' }).click()
  await expect(page.getByText('查询哪个时间范围？', { exact: true })).toBeVisible()
  expect(submitted).toEqual({ answers: { metric: '按贷款账户去重，并排除已结清账户' }, draft_version: 1 })
  await expect(page.getByText('实际执行 SQL', { exact: false })).toHaveCount(0)
})

test('accepted clarification streams Agent progress and the next card', async ({ page }) => {
  const card = { id: 'metric', field: 'metric', question: '如何统计贷款客户？', reason: '影响去重口径', options: [], allow_free_text: true, blocking: true }
  const draft = { version: 1, summary: '客户统计待确认', subject: '客户', metrics: [], dimensions: [], filters: [], output_fields: [], aggregation_rules: [], assumptions: [], analysis_steps: [], clarifications: [card] }
  let current: any = { id: 'async-run', session_id: 'async-session', domain: 'bank_analysis', question: '统计贷款客户', status: 'clarifying', events: [], evidence: [], requirement_draft: draft }
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'async-session', title: '客户分析', domain: 'bank_analysis', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'bank_analysis', name: '银行数据分析' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
    if (path === '/api/v1/sessions/async-session/runs') return route.fulfill({ json: [current] })
    if (path === '/api/v1/runs/async-run/alignment') {
      current = { ...current, status: 'understanding' }
      return route.fulfill({ status: 202, json: current })
    }
    if (path === '/api/v1/runs/async-run/events') {
      const event = { event: 'agent.tool.completed', status: 'understanding', created_at: new Date().toISOString(), data: { agent: 'analyst', tool: 'read_object', summary: '读取本体对象，发现多个机构角色' } }
      const events = [
        { ...event, event: 'agent.model.started', data: { agent: 'analyst', iteration: 1, phase: '首次需求分析', summary: '第 1 轮分析' } },
        { ...event, event: 'agent.model.started', data: { agent: 'analyst', iteration: 1, phase: '补充需求分析', summary: '第 1 轮分析' } },
        { ...event, event: 'agent.tool.started', data: { agent: 'analyst', tool: 'read_object', summary: '调用 read_object', analysis_goal: '核实机构归属口径', lookup: { name: 'loan' } } },
        event,
      ]
      current = { ...current, status: 'clarifying', events, requirement_draft: { ...draft, version: 2, clarifications: [{ ...card, id: 'org', question: '按经办机构还是入账机构统计？' }] } }
      return route.fulfill({ contentType: 'text/event-stream', body: events.map(item => `event: ${item.event}\ndata: ${JSON.stringify(item)}\n\n`).join('') + 'event: stream.closed\ndata: {"status":"clarifying"}\n\n' })
    }
    if (path === '/api/v1/runs/async-run') return route.fulfill({ json: current })
    return route.continue()
  })
  await page.goto('/workbench')
  await page.getByPlaceholder('请输入业务口径').fill('按客户去重')
  await page.getByRole('button', { name: '提交补充，继续分析' }).click()
  await expect(page.getByText('按经办机构还是入账机构统计？', { exact: true })).toBeVisible()
  await expect(page.getByText('读取本体对象，发现多个机构角色', { exact: true })).toHaveCount(0)
  const agentPanel = page.getByRole('button', { name: 'Agent 多轮分析与协作', exact: false })
  await expect(agentPanel).toHaveAttribute('aria-expanded', 'false')
  await agentPanel.click()
  await expect(page.getByText('读取本体对象，发现多个机构角色', { exact: true })).toBeVisible()
  await expect(page.getByText('多轮分析与协作', { exact: false })).toBeVisible()
  await expect(page.getByText('任务第 2 轮：补充需求分析，核验需求与证据并选择下一步', { exact: true })).toBeVisible()
  await expect(page.getByText('核实机构归属口径', { exact: false })).toBeVisible()
  await agentPanel.click()
  await expect(agentPanel).toHaveAttribute('aria-expanded', 'false')
})

test('rejected SQL stays in history and the revised confirmation remains prominent', async ({ page }) => {
  const sql = { physical_sql: 'SELECT COUNT(*) FROM loans', semantic_sql: 'SELECT COUNT(*) FROM loan', artifact_hash: 'rejected-sql-v1', dialect: 'mysql', parameters: {} }
  const draft = { version: 2, summary: '修订后仅统计有效贷款', subject: '贷款', metrics: [], dimensions: [], filters: [], output_fields: [], assumptions: [], analysis_steps: [], clarifications: [] }
  const events: any[] = [
    { event: 'sql.ready', status: 'compiling', created_at: '2026-11-03T10:00:00Z', data: { sql } },
    { event: 'review.completed', status: 'reviewing', created_at: '2026-11-03T10:00:01Z', data: { review: { decision: 'revise', rationale: '缺少有效状态过滤条件' } } },
    { event: 'review.revision.required', status: 'reviewing', created_at: '2026-11-03T10:00:02Z', data: { summary: '缺少有效状态过滤条件' } },
    { event: 'requirement.revised', status: 'aligning', created_at: '2026-11-03T10:00:03Z', data: { requirement: draft } },
  ]
  for (let i = 0; i < 40; i++) events.push({ event: 'agent.tool.completed', status: 'understanding', created_at: `trace-${i}`, data: { agent: 'analyst', summary: `历史工具记录 ${i}` } })
  const run = { id: 'revised-run', session_id: 'revised-session', domain: 'bank_analysis', question: '贷款统计', status: 'clarifying', revision_count: 1, events, evidence: [], requirement_draft: draft }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'revised-session', title: '贷款统计', domain: 'bank_analysis', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'bank_analysis', name: '银行数据分析' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
    if (path === '/api/v1/sessions/revised-session/runs') return route.fulfill({ json: [run] })
    return route.continue()
  })
  await page.goto('/workbench')
  await expect(page.getByText('当前需要您确认', { exact: true })).toBeVisible()
  await expect(page.getByText('历史工具记录 0', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '查看待确认口径' }).click()
  await expect(page.getByRole('button', { name: '确认修订方案，重新生成 SQL' })).toBeVisible()
  const history = page.getByRole('button', { name: '历史 SQL', exact: false })
  await expect(history).toHaveAttribute('aria-expanded', 'false')
  await history.click()
  await expect(page.getByText('SELECT COUNT(*) FROM loans', { exact: true })).toBeVisible()
  await expect(page.getByText('非最终交付', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '当前 SQL', exact: false })).toHaveCount(0)
})

test('a delayed SQL snapshot cannot overwrite a newer revision card', async ({ page }) => {
  const card = { id: 'time', field: 'time', question: '统计哪个时间范围？', reason: '影响范围', options: [], allow_free_text: true, blocking: true }
  const draft = { version: 1, summary: '贷款统计', metrics: [], dimensions: [], filters: [], output_fields: [], assumptions: [], analysis_steps: [], clarifications: [card] }
  const sql = { physical_sql: 'SELECT COUNT(*) FROM loans', artifact_hash: 'old-sql', dialect: 'mysql', parameters: {} }
  const event = (name: string, status: string, data: any, index: number) => ({ event: name, status, data, created_at: `2026-11-03T10:00:0${index}Z` })
  const events = [
    event('sql.ready', 'compiling', { sql }, 0),
    event('review.completed', 'reviewing', { review: { decision: 'revise', rationale: '缺少过滤条件' } }, 1),
    event('review.revision.required', 'reviewing', { summary: '缺少过滤条件' }, 2),
    event('alignment.required', 'clarifying', {}, 3),
  ]
  const initial = { id: 'race-run', session_id: 'race-session', domain: 'bank_analysis', question: '贷款统计', status: 'clarifying', events: [], evidence: [], requirement_draft: draft }
  const final = { ...initial, revision_count: 1, events, requirement_draft: { ...draft, version: 2, summary: '修订后的贷款统计', clarifications: [] } }
  let snapshots = 0
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'race-session', title: '贷款统计', domain: 'bank_analysis', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'bank_analysis', name: '银行数据分析' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'risk_based' } })
    if (path === '/api/v1/sessions/race-session/runs') return route.fulfill({ json: [initial] })
    if (path === '/api/v1/runs/race-run/alignment') return route.fulfill({ status: 202, json: { ...initial, status: 'understanding' } })
    if (path === '/api/v1/runs/race-run/events') return route.fulfill({ contentType: 'text/event-stream', body: events.map((item, i) => `id: ${i + 1}\nevent: ${item.event}\ndata: ${JSON.stringify(item)}\n\n`).join('') + 'event: stream.closed\ndata: {}\n\n' })
    if (path === '/api/v1/runs/race-run') {
      snapshots += 1
      if (snapshots === 1) {
        await new Promise(resolve => setTimeout(resolve, 150))
        return route.fulfill({ json: { ...initial, status: 'compiling', sql, requirement: { summary: '旧口径' }, events: [events[0]] } })
      }
      return route.fulfill({ json: final })
    }
    return route.continue()
  })
  await page.goto('/workbench')
  await page.getByPlaceholder('请输入业务口径').fill('全部时间')
  await page.getByRole('button', { name: '提交补充，继续分析' }).click()
  // Let the deliberately delayed old response arrive after the SSE burst.
  await page.waitForTimeout(350)
  await expect(page.getByRole('button', { name: '确认修订方案，重新生成 SQL' })).toBeVisible()
  await expect(page.getByText('修订后的贷款统计', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '当前 SQL', exact: false })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '历史 SQL', exact: false })).toBeVisible()
})
