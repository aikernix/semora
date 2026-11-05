import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'

async function login(page: Page, username: string, password: string) {
  await page.goto('/workbench')
  await expect(page).toHaveURL(/\/realms\/semora\/protocol\/openid-connect\/auth/)
  await page.locator('#username').fill(username)
  await page.locator('#password').fill(password)
  await page.locator('#kc-login').click()
  await expect(page).toHaveURL(/\/workbench$/)
  await expect(page.getByText('SEMORA', { exact: true })).toBeVisible()
}

async function accessToken(request: APIRequestContext, username: string, password: string) {
  const response = await request.post(
    'http://localhost:8088/realms/semora/protocol/openid-connect/token',
    { form: { grant_type: 'password', client_id: 'semora-web', username, password } },
  )
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token as string
}

test('same-role users cannot access another user session or run', async ({ request }) => {
  const ownerToken = await accessToken(request, 'demo', 'demo1234')
  const otherToken = await accessToken(request, 'demo-other', 'other1234')
  const ownerHeaders = { Authorization: `Bearer ${ownerToken}` }
  const otherHeaders = { Authorization: `Bearer ${otherToken}` }
  const title = `资源隔离 ${Date.now()}`

  const createResponse = await request.post('http://localhost:8000/api/v1/sessions', {
    headers: ownerHeaders,
    data: { title, domain: 'retail_credit' },
  })
  expect(createResponse.status()).toBe(201)
  const session = await createResponse.json()

  const otherSessions = await request.get('http://localhost:8000/api/v1/sessions', { headers: otherHeaders })
  expect((await otherSessions.json()).some((item: any) => item.id === session.id)).toBeFalsy()
  const forbiddenMessage = await request.post(`http://localhost:8000/api/v1/sessions/${session.id}/messages`, {
    headers: otherHeaders,
    data: { question: '查询贷款余额' },
  })
  expect(forbiddenMessage.status()).toBe(404)

  const runResponse = await request.post(`http://localhost:8000/api/v1/sessions/${session.id}/messages`, {
    headers: ownerHeaders,
    data: { question: '客户数' },
  })
  expect(runResponse.status()).toBe(202)
  const run = await runResponse.json()
  const ownerHistory = await request.get(`http://localhost:8000/api/v1/sessions/${session.id}/runs?limit=1`, { headers: ownerHeaders })
  expect(ownerHistory.status()).toBe(200)
  expect((await ownerHistory.json())[0].id).toBe(run.id)
  expect((await request.get(`http://localhost:8000/api/v1/sessions/${session.id}/runs`, { headers: otherHeaders })).status()).toBe(404)
  expect((await request.get(`http://localhost:8000/api/v1/runs/${run.id}`, { headers: otherHeaders })).status()).toBe(404)
  expect((await request.post(`http://localhost:8000/api/v1/executions/${run.id}/cancel`, { headers: otherHeaders })).status()).toBe(404)
  expect((await request.get(`http://localhost:8000/api/v1/audits/${run.id}`, { headers: otherHeaders })).status()).toBe(403)
  expect((await request.get(`http://localhost:8000/api/v1/audits/${run.id}/verify`, { headers: otherHeaders })).status()).toBe(403)
  expect((await request.get(`http://localhost:8000/api/v1/audits/${run.id}/archive/verify`, { headers: otherHeaders })).status()).toBe(403)
})

test('admin completes knowledge, ontology, query, approval and audit flow', async ({ page }, testInfo) => {
  const ocrFixture = testInfo.outputPath('semora-ocr-bank-policy.png')
  await page.setContent(`
    <main style="width:1400px;height:230px;background:white;color:black;padding:30px;font-family:Arial,sans-serif">
      <div style="font-size:58px;font-weight:700">SEMORA OCR BANK POLICY 2026</div>
      <div style="font-size:52px;margin-top:28px">LOAN RISK REVIEW REQUIRED</div>
    </main>
  `)
  await page.locator('main').screenshot({ path: ocrFixture })
  await login(page, 'demo-admin', 'admin1234')
  await expect(page.getByRole('button', { name: '系统设置' })).toBeVisible()

  await page.getByRole('button', { name: '知识库' }).click()
  await expect(page.getByRole('heading', { name: '知识库' })).toBeVisible()
  await expect(page.locator('.folder').first()).toBeVisible()
  const fixture = fileURLToPath(
    new URL('../../../services/knowledge/seed/retail-credit-metrics.md', import.meta.url),
  )
  const uploadResponse = page.waitForResponse(
    response => response.url().includes('/knowledge/file') && response.request().method() === 'POST',
    { timeout: 30_000 },
  )
  await page.locator('input[type=file]').setInputFiles(fixture)
  expect((await uploadResponse).status()).toBe(202)
  const uploadedDocument = page.locator('.document-row').filter({ hasText: 'retail-credit-metrics' }).first()
  await expect(uploadedDocument).toBeVisible()
  await expect(uploadedDocument.getByText('completed', { exact: true })).toBeVisible()
  const downloadPromise = page.waitForEvent('download')
  await uploadedDocument.getByRole('button', { name: '下载原文' }).click()
  expect((await downloadPromise).suggestedFilename()).toBe('retail-credit-metrics.md')
  await uploadedDocument.getByRole('button', { name: '抽取本体候选' }).click()
  await expect(page.getByText(/已同步 \d+ 个本体候选/)).toBeVisible()

  const ocrUploadResponse = page.waitForResponse(
    response => response.url().includes('/knowledge/file') && response.request().method() === 'POST',
    { timeout: 30_000 },
  )
  await page.locator('input[type=file]').setInputFiles(ocrFixture)
  expect((await ocrUploadResponse).status()).toBe(202)
  const ocrDocument = page.locator('.document-row').filter({ hasText: 'semora-ocr-bank-policy' }).first()
  await expect(ocrDocument).toBeVisible()
  await expect(ocrDocument.getByText('completed', { exact: true })).toBeVisible({ timeout: 60_000 })
  await page.getByPlaceholder('检索业务制度、指标口径或数据安全规则').fill('SEMORA OCR BANK POLICY 2026')
  await page.getByRole('button', { name: '混合检索' }).click()
  const ocrEvidence = page.locator('.result-grid article').filter({ hasText: 'semora-ocr-bank-policy' }).first()
  await expect(ocrEvidence).toContainText('SEMORA OCR BANK POLICY 2026')

  await page.getByRole('button', { name: '本体工作室' }).click()
  await expect(page.getByRole('heading', { name: '本体工作室' })).toBeVisible()
  const metricCandidate = page.locator('.candidate').filter({ hasText: '贷款余额' }).first()
  await expect(metricCandidate).toBeVisible()
  const acceptButton = metricCandidate.getByRole('button', { name: '接受候选' })
  if (await acceptButton.isVisible()) {
    page.once('dialog', dialog => dialog.accept('Playwright 证据与结构化映射核对通过'))
    await acceptButton.click()
    await expect(page.getByText(/候选已合并到草稿/)).toBeVisible()
  } else {
    await expect(metricCandidate.getByText('accepted', { exact: true })).toBeVisible()
  }
  await page.getByRole('button', { name: '发布当前草稿' }).click()
  await expect(page.getByText('本体发布成功')).toBeVisible()

  await page.getByRole('button', { name: '查询工作台' }).click()
  const aggregateResponse = page.waitForResponse(
    response => response.url().includes('/messages') && response.request().method() === 'POST',
    { timeout: 30_000 },
  )
  await page.getByRole('button', { name: /最近12个月各分行贷款余额和90\+逾期率/ }).click()
  const aggregateRun = await (await aggregateResponse).json()
  expect(aggregateRun.status).toBe('received')
  await expect(page.getByText('查询结果', { exact: true })).toBeVisible()
  await expect(page.getByText('自动图表', { exact: true })).toBeVisible()
  await expect(page.getByText(/结果 Reviewer：approved/)).toBeVisible()
  await expect(page.getByText(/已按查询口径和授权机构范围完成统计/)).toBeVisible()
  await page.getByRole('button', { name: '本体工作室' }).click()
  await page.getByRole('button', { name: '查询工作台' }).click()
  await page.locator('.session-item').filter({ hasText: '零售信贷经营分析' }).first().click()
  await expect(page.getByText('查询结果', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '审计回放' }).click()
  const indexedRun = page.getByRole('button', { name: `回放 ${aggregateRun.id}` })
  await expect(indexedRun).toBeVisible()
  await indexedRun.click()
  await expect(page.getByText('数据库哈希链完整')).toBeVisible()
  await expect(page.getByText(/sha256-chain/)).toBeVisible()
  await expect(async () => {
    if (await page.getByText('尚未完成外部归档').isVisible()) {
      await page.waitForTimeout(2_000)
      await page.getByRole('button', { name: '验证并回放' }).click()
    }
    await expect(page.getByText('外部归档校验通过')).toBeVisible()
  }).toPass({ timeout: 20_000, intervals: [1_000, 2_000] })
  await expect(page.getByText('不可变归档凭证')).toBeVisible()
  await expect(page.getByText(/s3:\/\/semora-audit\/audit\//)).toBeVisible()
  await expect(page.getByText('内容 SHA-256')).toBeVisible()

  await page.goto('/workbench')
  const detailResponse = page.waitForResponse(
    response => response.url().includes('/messages') && response.request().method() === 'POST',
    { timeout: 30_000 },
  )
  await page.getByRole('button', { name: /查询贷款客户明细和手机号/ }).click()
  expect((await detailResponse).status()).toBe(202)
  await expect(page.getByText('该查询需要人工审批')).toBeVisible()
  await page.getByRole('button', { name: '批准并执行' }).click()
  await expect(page.getByText('查询结果', { exact: true })).toBeVisible()
  await expect(page.getByText(/结果 Reviewer：approved/)).toBeVisible()
  await expect(page.getByText('138****1001')).toBeVisible()
})
