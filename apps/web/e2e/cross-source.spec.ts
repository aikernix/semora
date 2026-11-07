import { expect, test } from '@playwright/test'

test('cross-source queries expose both SQL steps, dependencies and final results', async ({ page }) => {
  const source_steps = [
    { name: 'pick', label: '选择客户', data_source_id: 'source-a', query: { intent: '查询有效客户编号' }, inputs: [] },
    { name: 'lookup', label: '查账户', data_source_id: 'source-b', query: { intent: '查询这些客户的账户余额' }, inputs: [{ source_step: 'pick', source_field: 'customer.id', target_field: 'account.customer_id' }] },
  ]
  const artifact = (sql: string) => ({ physical_sql: sql, parameters: { p: 1 }, compilation: { inputs_bound: true } })
  const run = { id: 'source-run', session_id: 'source-session', domain: 'commerce', question: '查客户账户余额', status: 'completed', evidence: [],
    plan: { version: '3.0', metrics: [], dimensions: [], fields: ['account.amount'], relationship_path: [], assumptions: [], source_steps },
    source_artifacts: { pick: artifact('SELECT customer_id FROM customers LIMIT 1001'), lookup: artifact('SELECT amount FROM accounts WHERE customer_id IN (%(p)s) LIMIT 1000') },
    sql: artifact('SELECT amount FROM accounts LIMIT 1000'), gate: { allowed: true }, review: { decision: 'approved' },
    result: { columns: ['amount'], rows: [[100]], row_count: 1, duration_ms: 5, truncated: false }, answer: '已完成跨源查询',
    events: source_steps.map(step => ({ event: 'source.step.completed', status: 'executing', created_at: '2026-11-05T01:00:00Z', data: { step: step.name } })) }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'source-session', title: '跨源查询', domain: 'commerce', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'commerce', name: '跨源演示' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/settings') return route.fulfill({ json: { approval_mode: 'disabled', require_plan_confirmation: false } })
    if (path === '/api/v1/sessions/source-session/runs') return route.fulfill({ json: [run] })
    return route.fulfill({ json: {} })
  })
  await page.goto('/workbench')
  await expect(page.getByText('分步查询计划', { exact: false })).toBeVisible()
  await expect(page.getByText('选择客户 · 完成', { exact: true })).toBeVisible()
  await expect(page.getByText('查账户 · 完成', { exact: true })).toBeVisible()
  await expect(page.getByText('从 pick 传递 customer.id', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '分步查询计划', exact: false }).click()
  await expect(page.getByText('SELECT customer_id FROM customers LIMIT 1001', { exact: true })).toBeVisible()
  await expect(page.getByText('SELECT amount FROM accounts WHERE customer_id IN (%(p)s) LIMIT 1000', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '当前 SQL', exact: false })).toHaveCount(0)
  await expect(page.locator('.result-table')).toContainText('100')
})

test('cross-source SQL is hidden until the whole plan passes review', async ({ page }) => {
  const step = { name: 'pick', label: '选择客户', data_source_id: 'source-a', query: { intent: '查询客户编号' }, inputs: [] }
  const run = { id: 'rejected-run', session_id: 'source-session', domain: 'commerce', question: '查询客户', status: 'failed', evidence: [], events: [],
    plan: { metrics: [], dimensions: [], fields: [], relationship_path: [], assumptions: [], source_steps: [step] },
    gate: { allowed: true }, review: { decision: 'rejected' }, error: '复核未通过',
    source_artifacts: { pick: { physical_sql: 'SELECT unreviewed_field FROM private_records LIMIT 1000', parameters: {}, compilation: {} } } }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'source-session', title: '跨源查询', domain: 'commerce', run_count: 1 }] })
    if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'commerce', name: '跨源演示' }] })
    if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
    if (path === '/api/v1/sessions/source-session/runs') return route.fulfill({ json: [run] })
    return route.fulfill({ json: {} })
  })
  await page.goto('/workbench')
  await page.getByRole('button', { name: '分步查询计划', exact: false }).click()
  await expect(page.getByText('SELECT unreviewed_field FROM private_records LIMIT 1000', { exact: true })).toHaveCount(0)
  await expect(page.getByText('复核未通过', { exact: true })).toBeVisible()
})

test('ontology shows object data sources and identifies cross-instance relations', async ({ page }) => {
  const draft = { domain: 'commerce', name: '跨源演示', version: '1.0.0', dataSourceId: 'a', metrics: [], knowledgeBaseIds: [],
    objects: [{ name: 'client', label: '客户', table: 'customers', primaryKey: 'id', dataSourceId: 'a', properties: [] },
              { name: 'account', label: '账户', table: 'accounts', primaryKey: 'id', dataSourceId: 'b', properties: [] }],
    relations: [{ name: 'lookup', from: 'client', to: 'account', cardinality: '1:N', condition: 'client.id = account.customer_id' }] }
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontology/drafts') return route.fulfill({ json: [{ domain: 'commerce' }] })
    if (path === '/api/v1/ontology/drafts/commerce') return route.fulfill({ json: draft })
    if (path === '/api/v1/datasources') return route.fulfill({ json: [{ id: 'a', name: '客户库', type: 'mysql' }, { id: 'b', name: '账户库', type: 'postgresql' }] })
    if (path === '/api/v1/ontology/publication-status') return route.fulfill({ json: { published_version: '1.0.0', published_counts: { objects: 2, relations: 1, metrics: 0 } } })
    return route.fulfill({ json: [] })
  })
  await page.goto('/ontology')
  await expect(page.getByText('跨源分步查询', { exact: false })).toBeVisible()
  await expect(page.locator('.object-card').first().getByRole('textbox')).toHaveValue('客户库 · mysql')
  await expect(page.locator('.object-card').last().getByRole('textbox')).toHaveValue('账户库 · postgresql')
  await expect(page.getByRole('button', { name: '保存数据源映射', exact: true })).toBeDisabled()
})
