import { test, expect } from '@playwright/test'

for (const blocked of [false, true]) {
  test(blocked ? 'blocked request preserves SQL and a continuation response' : 'advisory review delivers results without displaying approval', async ({ page }) => {
    const run = {
      id: 'delivery-run', session_id: 'delivery-session', domain: 'generic', question: '查询当前记录',
      status: blocked ? 'failed' : 'completed', evidence: [], events: [],
      error: blocked ? '数据源暂不可用' : null,
      sql: { physical_sql: 'SELECT 1 AS value', dialect: 'postgres', artifact_hash: 'synthetic-delivery-hash', parameters: {} },
      gate: { allowed: true, risk_level: 'low', checks: [] },
      review: { decision: 'revise', disposition: 'advisory', rationale: '排序可以进一步核对' },
      result_review: blocked ? null : { decision: 'revise', disposition: 'advisory', reviewer_mode: 'llm', rationale: '请核对展示口径' },
      result: blocked ? null : { columns: ['value'], rows: [[1]], row_count: 1, duration_ms: 2 },
      answer: blocked ? '已生成SQL，但数据源暂不可用。' : '查询返回1条结果，附有待核对项。',
      delivery: { kind: blocked ? 'blocked' : 'result', title: blocked ? '当前交付与继续方式' : '查询结果（含待核对项）',
        summary: blocked ? '已生成SQL，但数据源暂不可用。' : '查询返回1条结果。',
        warnings: ['排序口径待核对'], next_action: '可以继续补充当前需求。', result_verified: false },
    }
    await page.route('**/api/v1/**', route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/v1/sessions') return route.fulfill({ json: [{ id: 'delivery-session', title: '尽力交付测试', domain: 'generic', run_count: 1 }] })
      if (path === '/api/v1/domains') return route.fulfill({ json: [{ domain: 'generic', name: '通用示例' }] })
      if (path === '/api/v1/model/status') return route.fulfill({ json: { configured: true } })
      if (path === '/api/v1/settings') return route.fulfill({ json: { delivery_mode: 'best_effort', approval_mode: 'disabled' } })
      if (path === '/api/v1/sessions/delivery-session/runs') return route.fulfill({ json: [run] })
      return route.fulfill({ json: {} })
    })
    await page.goto('/workbench')
    await expect(page.getByText('排序口径待核对', { exact: true })).toBeVisible()
    await expect(page.locator('.composer textarea')).toBeVisible()
    if (blocked) {
      await expect(page.getByText('已生成SQL，但数据源暂不可用。', { exact: true })).toBeVisible()
      await expect(page.getByText('查看技术详情', { exact: true })).toBeVisible()
    } else {
      await expect(page.locator('.result-table tbody tr')).toHaveCount(1)
      await expect(page.getByText('带建议交付', { exact: false })).toBeVisible()
      await expect(page.getByText('复核建议（结果已交付）', { exact: true })).toBeVisible()
      await expect(page.getByText('复核通过', { exact: false })).toHaveCount(0)
    }
    await page.screenshot({ path: `test-results/delivery-${blocked ? 'blocked' : 'result'}.png`, fullPage: true })
  })
}
