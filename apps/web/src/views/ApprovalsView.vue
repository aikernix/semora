<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type SystemSettings } from '../api'

const approvals = ref<any[]>([])
const settings = ref<SystemSettings | null>(null)
const loading = ref('')

async function load() {
  const [approvalResponse, settingsResponse] = await Promise.all([
    api.get('/approvals'),
    api.get<SystemSettings>('/settings'),
  ])
  approvals.value = approvalResponse.data
  settings.value = settingsResponse.data
}

async function decide(item: any, approved: boolean) {
  loading.value = item.id
  try {
    await api.post(`/approvals/${item.id}/decision`, {
      approved,
      reason: approved ? '业务用途和机构范围已核对' : '用途说明不足',
    })
    await load()
  } finally { loading.value = '' }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div><h1>审批中心</h1><p>人工审批与 SQL 哈希、权限、本体和策略版本绑定；任一变化都会使审批失效。</p></div>
      <t-button variant="outline" @click="load">刷新</t-button>
    </div>
    <div class="stat-grid">
      <div class="stat-card"><span>待审批</span><strong>{{ approvals.filter(x => x.status === 'pending').length }}</strong></div>
      <div class="stat-card"><span>已批准</span><strong>{{ approvals.filter(x => x.status === 'approved').length }}</strong></div>
      <div class="stat-card"><span>已拒绝</span><strong>{{ approvals.filter(x => x.status === 'rejected').length }}</strong></div>
      <div class="stat-card"><span>当前策略</span><strong class="policy-name">{{ settings?.approval_mode || '—' }}</strong><small>{{ settings?.policy_version }}</small></div>
    </div>
    <div class="panel">
      <div class="panel-title">审批任务</div>
      <div v-if="!approvals.length" class="empty-hint">暂无审批任务。在查询工作台运行“查询贷款客户明细和手机号”可创建一条。</div>
      <div v-for="item in approvals" :key="item.id" class="approval-row">
        <div>
          <strong>敏感贷款明细查询</strong>
          <small>Run {{ item.run_id.slice(0, 8) }} · {{ item.policy_version }}</small>
          <code>{{ item.binding_hash?.slice(0, 18) || item.sql_hash.slice(0, 18) }}…</code>
        </div>
        <t-tag :theme="item.status === 'pending' ? 'warning' : item.status === 'approved' ? 'success' : 'danger'">{{ item.status }}</t-tag>
        <div v-if="item.status === 'pending'">
          <t-button variant="outline" @click="decide(item, false)">拒绝</t-button>
          <t-button theme="primary" :loading="loading === item.id" @click="decide(item, true)">批准</t-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.approval-row{display:grid;grid-template-columns:1fr 100px 160px;align-items:center;padding:18px 20px;border-bottom:1px solid var(--semora-line)}.approval-row small,.approval-row code{display:block;color:var(--semora-muted);margin-top:6px}.approval-row .t-button{margin-left:7px}.policy-name{font-size:16px!important}.stat-card small{display:block;color:var(--semora-muted);font-size:10px;margin-top:7px;overflow:hidden;text-overflow:ellipsis}
</style>
