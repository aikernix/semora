import { test, expect, type Page } from '@playwright/test'

async function studio(page: Page) {
  const configuration = {
    domain: 'fixture', name: '关联演示', version: '1.0.0',
    objects: [
      { name: 'customer', label: '客户', table: 'demo.customer', dataSourceId: 'db', primaryKey: 'id', properties: [{ name: 'id', column: 'id', type: 'string' }] },
      { name: 'loan', label: '贷款', table: 'demo.loan', dataSourceId: 'db', primaryKey: 'id', properties: [{ name: 'customer_id', column: 'customer_id', type: 'string' }] },
    ],
    metrics: [{ name: 'loan_count', expression: 'COUNT(loan.id)', grain: 'customer' }],
    relations: [{ name: 'customer_loan', label: '客户持有贷款', from: 'customer', to: 'loan', cardinality: '1:N', joinType: 'one_to_many', condition: 'customer.id = loan.customer_id', metadata: { origin: 'manual', compound: true } }],
  }
  let current: any = structuredClone(configuration)
  const writes: any[] = []
  let rejectNext = false
  await page.route('**/api/v1/ontology/configurations*', async route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      writes.push(body)
      if (rejectNext) { rejectNext = false; await route.fulfill({ status: 422, json: { detail: '关联条件引用了不存在的对象' } }); return }
      current = structuredClone(body)
      await route.fulfill({ json: current })
    } else await route.fulfill({ json: [{ domain: 'fixture', name: '关联演示' }] })
  })
  await page.route('**/api/v1/ontology/configurations/fixture', async route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON()
      writes.push(body)
      if (rejectNext) { rejectNext = false; await route.fulfill({ status: 422, json: { detail: '关联条件引用了不存在的对象' } }); return }
      current = structuredClone(body)
    }
    await route.fulfill({ json: current })
  })
  await page.route('**/api/v1/datasources', route => route.fulfill({ json: [{ id: 'db', name: '演示库', type: 'mysql' }] }))
  await page.goto('/ontology')
  await expect(page.getByRole('heading', { name: '本体工作室' })).toBeVisible()
  await expect(page.locator('.relation-card')).toHaveCount(1)
  return { configuration, writes, reject: () => { rejectNext = true } }
}

test('studio shows only table relationships and edits persist without changing mappings or metrics', async ({ page }) => {
  const { configuration, writes, reject } = await studio(page)
  await expect(page.getByText('对象与字段映射', { exact: true })).toHaveCount(0)
  await expect(page.getByText('指标目录', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '编辑本体', exact: true })).toHaveCount(0)
  await expect(page.locator('.table-node').first()).toContainText('demo.customer')
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  const dialog = page.locator('.t-dialog:visible').filter({ hasText: '编辑关联关系' })
  await dialog.locator('label').filter({ hasText: '关系名称' }).locator('input').fill('客户贷款关联')
  await dialog.locator('textarea').fill('customer.id = loan.customer_id AND loan.customer_id IS NOT NULL')
  reject()
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(dialog.getByText('关联条件引用了不存在的对象', { exact: true })).toBeVisible()
  await expect(page.locator('.relation-card')).toContainText('客户持有贷款')
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(dialog).toHaveCount(0)
  expect(writes.at(-1).objects).toEqual(configuration.objects)
  expect(writes.at(-1).metrics).toEqual(configuration.metrics)
  expect(writes.at(-1).relations[0].metadata).toEqual(configuration.relations[0].metadata)
  await page.reload()
  await expect(page.locator('.relation-card')).toContainText('客户贷款关联')
  await expect(page.locator('.relation-condition')).toContainText('AND loan.customer_id IS NOT NULL')
  await page.screenshot({ path: '../../artifacts/ontology-relations/studio.png', fullPage: true })
})

test('relations can be added, cancelled and deleted with confirmation', async ({ page }) => {
  const { writes } = await studio(page)
  await page.getByRole('button', { name: '编辑', exact: true }).click()
  let dialog = page.locator('.t-dialog:visible').filter({ hasText: '编辑关联关系' })
  await dialog.locator('textarea').fill('cancelled')
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  expect(writes).toHaveLength(0)
  await page.getByRole('button', { name: '新增关联', exact: true }).click()
  dialog = page.locator('.t-dialog:visible').filter({ hasText: '新增关联关系' })
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(dialog.getByText(/请填写关系标识/)).toBeVisible()
  await dialog.locator('label').filter({ hasText: '关系名称' }).locator('input').fill('补充关联')
  await dialog.locator('label').filter({ hasText: '关系标识' }).locator('input').fill('extra_link')
  await dialog.locator('label').filter({ hasText: '来源表' }).locator('.t-select').click()
  await page.getByText('客户 · demo.customer · 演示库 (customer)', { exact: true }).last().click()
  await dialog.locator('label').filter({ hasText: '目标表' }).locator('.t-select').click()
  await page.getByText('贷款 · demo.loan · 演示库 (loan)', { exact: true }).last().click()
  await dialog.locator('textarea').fill('customer.id = loan.customer_id')
  await dialog.getByRole('button', { name: '保存并生效' }).click()
  await expect(page.locator('.relation-card')).toHaveCount(2)
  await page.locator('.relation-card').filter({ hasText: '补充关联' }).getByRole('button', { name: '删除', exact: true }).click()
  const confirmation = page.locator('.t-dialog:visible').filter({ hasText: '删除关联关系' })
  await confirmation.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.locator('.relation-card')).toHaveCount(2)
  await page.locator('.relation-card').filter({ hasText: '补充关联' }).getByRole('button', { name: '删除', exact: true }).click()
  await confirmation.getByRole('button', { name: '删除并生效', exact: true }).click()
  await expect(page.locator('.relation-card')).toHaveCount(1)
  expect(writes.at(-1).relations.map((r: any) => r.name)).toEqual(['customer_loan'])
})
