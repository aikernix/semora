<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { DataBaseIcon, LinkIcon, RefreshIcon } from 'tdesign-icons-vue-next'
import { api } from '../api'

interface DataSource {
  id: string
  name: string
  type: string
  secret_ref: string
  status: string
  access: string
  objects: number
  last_scan_at?: string
}

interface ScanColumn {
  name: string
  type: string
  primary_key: boolean
  sensitivity: string
}

interface ScanTable {
  schema: string
  name: string
  columns: ScanColumn[]
}

const sources = ref<DataSource[]>([])
const scan = ref<any>(null)
const selected = ref<DataSource | null>(null)
const loadingId = ref('')
const error = ref('')
const showAdd = ref(false)
const creating = ref(false)
const form = ref({ name: '', type: 'postgresql', secret_ref: '' })

async function loadSources() {
  sources.value = (await api.get('/datasources')).data
}

async function testSource(source: DataSource) {
  error.value = ''
  loadingId.value = `test:${source.id}`
  try {
    const result = (await api.post(`/datasources/${source.id}/test`)).data
    scan.value = { connection: result, tables: [], foreign_keys: [], sensitive_columns: [] }
    selected.value = source
    await loadSources()
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || cause.message
  } finally {
    loadingId.value = ''
  }
}

async function doScan(source: DataSource) {
  error.value = ''
  loadingId.value = `scan:${source.id}`
  try {
    scan.value = (await api.post(`/datasources/${source.id}/scan`)).data
    selected.value = source
    await loadSources()
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || cause.message
  } finally {
    loadingId.value = ''
  }
}

async function createSource() {
  error.value = ''
  creating.value = true
  try {
    await api.post('/datasources', form.value)
    showAdd.value = false
    form.value = { name: '', type: 'postgresql', secret_ref: '' }
    await loadSources()
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || cause.message
  } finally {
    creating.value = false
  }
}

function statusTheme(status: string) {
  if (status === 'healthy') return 'success'
  if (status === 'unsafe') return 'danger'
  return 'warning'
}

function sensitivityTheme(level: string) {
  return level === 'restricted' ? 'danger' : level === 'confidential' ? 'warning' : 'default'
}

onMounted(loadSources)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>数据源</h1>
        <p>API 只保存密钥引用；数据库凭据由隔离 Executor 从环境或密钥管理系统解析。</p>
      </div>
      <t-button theme="primary" @click="showAdd = true">添加数据源</t-button>
    </div>

    <t-alert v-if="error" theme="error" title="数据源操作失败" :message="error" closable @close="error = ''" />

    <div class="panel source-list">
      <div class="source" v-for="source in sources" :key="source.id">
        <div class="db-icon"><DataBaseIcon size="26px" /></div>
        <div class="source-main">
          <strong>{{ source.name }}</strong>
          <small>{{ source.type }} · {{ source.secret_ref }} · {{ source.objects }} 个对象</small>
          <small v-if="source.last_scan_at">最近扫描 {{ new Date(source.last_scan_at).toLocaleString() }}</small>
        </div>
        <t-tag :theme="statusTheme(source.status) as any" variant="light">
          <i class="status-dot"></i>{{ source.status }} / {{ source.access }}
        </t-tag>
        <t-button variant="text" :loading="loadingId === `test:${source.id}`" @click="testSource(source)">
          <template #icon><LinkIcon /></template>连接测试
        </t-button>
        <t-button variant="outline" :loading="loadingId === `scan:${source.id}`" @click="doScan(source)">
          <template #icon><RefreshIcon /></template>扫描 Schema
        </t-button>
      </div>
    </div>

    <section v-if="scan" class="scan-panel">
      <div class="scan-head">
        <div>
          <h2>{{ selected?.name }} 扫描结果</h2>
          <p v-if="scan.connection">
            数据库 {{ scan.connection.database }} · 用户 {{ scan.connection.user }} ·
            默认只读 {{ scan.connection.default_transaction_read_only }}
          </p>
        </div>
        <t-tag :theme="scan.connection?.read_only ? 'success' : 'danger'">
          {{ scan.connection?.account_read_only === false ? '账号有写权限 · 执行强制只读' : scan.connection?.read_only ? '只读验证通过' : '账号存在写入风险' }}
        </t-tag>
      </div>

      <div class="scan-stats">
        <div><span>数据表</span><strong>{{ scan.tables?.length || 0 }}</strong></div>
        <div><span>外键关系</span><strong>{{ scan.foreign_keys?.length || 0 }}</strong></div>
        <div><span>敏感字段</span><strong>{{ scan.sensitive_columns?.length || 0 }}</strong></div>
      </div>

      <div v-if="scan.tables?.length" class="schema-grid">
        <article v-for="table in scan.tables as ScanTable[]" :key="`${table.schema}.${table.name}`">
          <header><DataBaseIcon /><strong>{{ table.schema }}.{{ table.name }}</strong><span>{{ table.columns.length }} 字段</span></header>
          <div class="column-row" v-for="column in table.columns" :key="column.name">
            <code>{{ column.name }}</code>
            <span>{{ column.type }}</span>
            <t-tag v-if="column.primary_key" size="small" theme="primary" variant="light">PK</t-tag>
            <t-tag v-if="column.sensitivity !== 'internal'" size="small" :theme="sensitivityTheme(column.sensitivity) as any" variant="light">
              {{ column.sensitivity }}
            </t-tag>
          </div>
        </article>
      </div>
    </section>

    <t-dialog v-model:visible="showAdd" header="添加数据源" :footer="false" width="560px">
      <div class="source-form">
        <label>数据源名称<t-input v-model="form.name" placeholder="例如：核心贷款只读库" /></label>
        <label>数据库类型
          <t-select v-model="form.type">
            <t-option value="postgresql" label="PostgreSQL（当前完整支持）" />
            <t-option value="oracle" label="Oracle（预留适配器）" />
            <t-option value="mysql" label="MySQL 8.0+" />
            <t-option value="sqlserver" label="SQL Server（预留适配器）" />
            <t-option value="trino" label="Hive / Trino（预留适配器）" />
          </t-select>
        </label>
        <label>Executor 密钥引用
          <t-input v-model="form.secret_ref" placeholder="DATASOURCE_CORE_LOAN_URL" />
          <small>这里只填写环境变量或密钥系统引用名，不填写 JDBC/DSN、用户名或密码。</small>
        </label>
        <div class="dialog-actions">
          <t-button variant="outline" @click="showAdd = false">取消</t-button>
          <t-button theme="primary" :loading="creating" :disabled="!form.name || !form.secret_ref" @click="createSource">保存</t-button>
        </div>
      </div>
    </t-dialog>
  </div>
</template>

<style scoped>
.source-list { margin-top: 18px; }
.source { display: flex; align-items: center; gap: 14px; padding: 19px 20px; border-bottom: 1px solid var(--semora-line); }
.source:last-child { border-bottom: 0; }
.db-icon { width: 50px; height: 50px; flex: none; border-radius: 10px; background: var(--semora-green-soft); color: var(--semora-green); display: grid; place-items: center; }
.source-main { flex: 1; min-width: 0; }
.source-main strong, .source-main small { display: block; }
.source-main small { color: var(--semora-muted); margin-top: 5px; }
.scan-panel { margin-top: 20px; border: 1px solid var(--semora-line); border-radius: 10px; overflow: hidden; }
.scan-head { padding: 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--semora-line); }
.scan-head h2 { margin: 0 0 7px; font-size: 19px; }.scan-head p { margin: 0; color: var(--semora-muted); }
.scan-stats { display: grid; grid-template-columns: repeat(3, 1fr); background: var(--semora-page); border-bottom: 1px solid var(--semora-line); }
.scan-stats div { padding: 17px 20px; border-right: 1px solid var(--semora-line); }.scan-stats div:last-child { border: 0; }
.scan-stats span { color: var(--semora-muted); }.scan-stats strong { display: block; margin-top: 7px; font-size: 23px; }
.schema-grid { padding: 18px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.schema-grid article { border: 1px solid var(--semora-line); border-radius: 9px; overflow: hidden; }
.schema-grid header { padding: 12px 14px; display: flex; gap: 8px; align-items: center; background: var(--semora-page); }
.schema-grid header strong { flex: 1; }.schema-grid header span { color: var(--semora-muted); font-size: 12px; }
.column-row { min-height: 39px; padding: 8px 13px; display: flex; align-items: center; gap: 8px; border-top: 1px solid var(--semora-line); }
.column-row code { flex: 1; font-size: 12px; }.column-row > span { color: var(--semora-muted); font-size: 12px; }
.source-form { display: flex; flex-direction: column; gap: 18px; padding-top: 5px; }
.source-form label { display: flex; flex-direction: column; gap: 8px; font-weight: 600; }.source-form small { color: var(--semora-muted); font-weight: 400; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 5px; }
@media (max-width: 1280px) { .schema-grid { grid-template-columns: 1fr; } }
</style>
