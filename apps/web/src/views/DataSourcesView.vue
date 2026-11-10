<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { hasRole } from '../auth'
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
  scan_job?: any
  capabilities?: Record<string, string>
  connection?: any
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
const editingId = ref('')
const connectionMessage = ref('')
const passwordConfigured = ref(false)
const products = ref<any[]>([])
const drivers = ref<any[]>([])
const showRelation = ref(false)
const relationCandidate = ref<any>(null)
const relationDomains = ref<any[]>([])
const relationObjects = ref<any[]>([])
const relationForm = ref({ domain: '', source_object: '', target_object: '' })
const relationMessage = ref('')
const showDrivers = ref(false)
const editingDriverId = ref('')
const driverFiles = ref<File[]>([])
const driverConfig = ref(JSON.stringify({ name: '', driver_class: '', version: '', enabled: false, dialect: '', readonly_strategy: '', pagination: 'baseline', type_mapping: {}, metadata_queries: {} }, null, 2))
const advancedProperties = ref('{}')
const scanScope = ref('')
const activeJob = ref<any>(null)
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const capabilityLabels: Record<string,string> = { connection:'连接',structure:'结构',comments:'注释',constraints:'主外键',mdl:'MDL',query:'查询' }
const stateLabels: Record<string,string> = {verified:'已验证',partial:'部分可用',pending:'待配置',failed:'失败'}
const emptyForm = () => ({ name: '', type: 'postgresql', host: '', port: 5432, database: '', username: '', password: '', schema_name: '', catalog: '', compatibility_mode: '', service_kind: 'service', jdbc_url: '', driver_id: '', product_override: '' })
const form = ref(emptyForm())

function addSource() {
  editingId.value = ''
  passwordConfigured.value = false
  connectionMessage.value = ''
  form.value = emptyForm()
  advancedProperties.value = '{}'
  showAdd.value = true
}

async function editSource(source: DataSource) {
  error.value = ''
  try {
    const connection = (await api.get(`/datasources/${source.id}/connection`)).data
    editingId.value = source.id
    passwordConfigured.value = !!connection.password_configured
    form.value = { ...emptyForm(), ...connection, name: source.name, type: source.type, password: '' }
    advancedProperties.value = JSON.stringify(connection.properties || {}, null, 2)
    connectionMessage.value = ''
    showAdd.value = true
  } catch (cause: any) { error.value = cause.response?.data?.detail || '无法读取连接配置' }
}

function connectionBody() {
  const { name, ...connection } = form.value
  // Do not forward public-only configuration flags back to the strict API contract.
  const keys = Object.keys(emptyForm()).filter(k => k !== 'name')
  const values = Object.fromEntries(keys.map(k => [k, (connection as any)[k]]))
  return { name, type: connection.type, connection: { ...values, port: Number(connection.port), properties: JSON.parse(advancedProperties.value || '{}') } }
}

async function testForm() {
  creating.value = true
  connectionMessage.value = ''
  try {
    const result = (await api.post('/datasource-connection-test', connectionBody(), { params: editingId.value ? { datasource_id: editingId.value } : {} })).data
    connectionMessage.value = `连接成功：${result.database}，${result.version || '数据库可访问'}。`
  } catch (cause: any) { connectionMessage.value = cause.response?.data?.detail || '连接测试失败' }
  finally { creating.value = false }
}

async function loadSources() {
  sources.value = (await api.get('/datasources')).data
  const running = sources.value.find(source => source.scan_job?.status === 'running')
  if (running && activeJob.value?.status !== 'running') {
    activeJob.value = { ...running.scan_job, sourceId: running.id }; selected.value = running
    void pollScan(running)
  }
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
    const job = (await api.post(`/datasources/${source.id}/scan-jobs`, { tables: scanScope.value.trim() ? scanScope.value.split(',').map(s => s.trim()).filter(Boolean) : null })).data
    activeJob.value = { ...job, sourceId: source.id }
    selected.value = source
    await pollScan(source)
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || cause.message
  } finally {
    loadingId.value = ''
  }
}

async function createSource() {
  error.value = ''
  connectionMessage.value = ''
  creating.value = true
  try {
    if (editingId.value) await api.put(`/datasources/${editingId.value}`, connectionBody())
    else await api.post('/datasources', connectionBody())
    showAdd.value = false
    scan.value = null
    selected.value = null
    form.value = emptyForm()
    await loadSources()
  } catch (cause: any) {
    connectionMessage.value = cause.response?.data?.detail || cause.message
  } finally {
    creating.value = false
  }
}

async function pollScan(source: DataSource) {
  if (disposed || !activeJob.value) return
  try {
    const job = (await api.get(`/datasources/${source.id}/scan-jobs/${activeJob.value.id}`)).data
    activeJob.value = { ...job, sourceId: source.id }
    if (job.status === 'running') { pollTimer = setTimeout(() => { void pollScan(source) }, 1500); return }
    if (job.status === 'failed') error.value = typeof job.error === 'string' ? job.error : JSON.stringify(job.error)
    scan.value = (await api.get(`/datasources/${source.id}/snapshot`)).data
    await loadSources()
  } catch (cause: any) { error.value = cause.response?.data?.detail || '无法获取扫描状态' }
}
async function cancelScan() {
  const job = activeJob.value
  if (job) { await api.post(`/datasources/${job.sourceId}/scan-jobs/${job.id}/cancel`); activeJob.value.status = 'cancelled' }
}
async function viewSnapshot(source: DataSource) {
  try { selected.value = source; scan.value = (await api.get(`/datasources/${source.id}/snapshot`)).data }
  catch (cause: any) { error.value = cause.response?.data?.detail || '无法读取结构' }
}
async function validateQuery(source: DataSource) {
  try { await api.post(`/datasources/${source.id}/validate-query`, {}, { timeout: 120000 }); await loadSources(); await viewSnapshot(source) }
  catch (cause: any) { error.value = JSON.stringify(cause.response?.data?.detail || '查询验证失败') }
}
async function downloadMdl() {
  if (!selected.value) return
  try {
    const report = (await api.get(`/datasources/${selected.value.id}/mdl-report`)).data
    scan.value = { ...scan.value, mdl_report: report }
    const result = await api.get(`/datasources/${selected.value.id}/mdl`, { responseType: 'blob' })
    const url = URL.createObjectURL(result.data); const link = document.createElement('a')
    link.href = url; link.download = 'semora-mdl.zip'; link.click(); URL.revokeObjectURL(url)
    await loadSources()
  } catch (cause: any) { error.value = cause.response?.data?.detail || 'MDL 导出失败' }
}
async function decideRelation(relation: any, status: string) {
  if (!selected.value) return
  try { await api.put(`/datasources/${selected.value.id}/relationships/${relation.id}`, { status }); relation.status = status }
  catch (cause: any) { error.value = cause.response?.data?.detail || '关系确认失败' }
}
async function profileRelation(relation: any) {
  if (!selected.value) return
  try { relation.profile = (await api.post(`/datasources/${selected.value.id}/relationships/${relation.id}/profile`)).data }
  catch (cause: any) { error.value = JSON.stringify(cause.response?.data?.detail || '采样验证失败') }
}
async function openRelation(relation: any) {
  relationCandidate.value = relation; relationMessage.value = ''; relationObjects.value = []
  relationForm.value = { domain:'', source_object:'', target_object:'' }; showRelation.value = true
  try { relationDomains.value = (await api.get('/ontology/configurations')).data }
  catch (cause: any) { relationMessage.value = cause.response?.data?.detail || '无法读取业务域' }
}
async function relationDomainChanged() {
  try {
    const config = (await api.get(`/ontology/configurations/${relationForm.value.domain}`)).data
    relationObjects.value = (config.objects || []).map((o: any) => ({ ...o, dataSourceId:o.dataSourceId || config.dataSourceId }))
    relationForm.value.source_object = relationObjects.value.find(o => o.dataSourceId === selected.value?.id && o.table === relationCandidate.value.source_table)?.name || ''
    relationForm.value.target_object = relationObjects.value.find(o => o.dataSourceId === selected.value?.id && o.table === relationCandidate.value.target_table)?.name || ''
  } catch (cause: any) { relationMessage.value = cause.response?.data?.detail || '无法读取对象映射' }
}
async function sendRelationCandidate() {
  try {
    await api.post(`/datasources/${selected.value?.id}/relationships/${relationCandidate.value.id}/ontology-candidate`, relationForm.value)
    relationMessage.value = '已提交到本体候选维护，请在本体维护中确认和保存。'
  } catch (cause: any) { relationMessage.value = cause.response?.data?.detail || '无法提交本体候选' }
}
async function installDriver() {
  try {
    JSON.parse(driverConfig.value)
    const data = new FormData(); data.append('config', driverConfig.value)
    driverFiles.value.forEach(file => data.append('files', file))
    if (editingDriverId.value) await api.put(`/settings/database-drivers/${editingDriverId.value}`, JSON.parse(driverConfig.value))
    else await api.post('/settings/database-drivers', data, { timeout: 200000 })
    drivers.value = (await api.get('/settings/database-drivers')).data
    connectionMessage.value = '驱动已保存；需先启用，再测试目标连接。'
  } catch (cause: any) { connectionMessage.value = JSON.stringify(cause.response?.data?.detail || cause.message) }
}
function editDriver(driver: any) {
  editingDriverId.value = driver.id
  const keys = ['name','driver_class','version','enabled','dialect','readonly_strategy','pagination','type_mapping','metadata_queries']
  driverConfig.value = JSON.stringify(Object.fromEntries(keys.filter(k => k in driver).map(k => [k,driver[k]])), null, 2)
}
async function toggleDriver(driver: any) {
  try {
    const keys = ['name','driver_class','version','enabled','dialect','readonly_strategy','pagination','type_mapping','metadata_queries']
    const config = Object.fromEntries(keys.filter(k => k in driver).map(k => [k, driver[k]]))
    config.enabled = !driver.enabled
    await api.put(`/settings/database-drivers/${driver.id}`, config)
    drivers.value = (await api.get('/settings/database-drivers')).data
  } catch (cause: any) { connectionMessage.value = JSON.stringify(cause.response?.data?.detail || cause.message) }
}
function selectProduct() {
  const product = products.value.find(p => p.id === form.value.type)
  form.value.port = product?.port || 5432; form.value.driver_id = product?.driver_id || ''
  form.value.compatibility_mode = form.value.type === 'oceanbase' ? 'mysql' : ''
}
onUnmounted(() => { disposed = true; if (pollTimer) clearTimeout(pollTimer) })

function statusTheme(status: string) {
  if (status === 'healthy') return 'success'
  if (status === 'unsafe') return 'danger'
  return 'warning'
}

function sensitivityTheme(level: string) {
  return level === 'restricted' ? 'danger' : level === 'confidential' ? 'warning' : 'default'
}

onMounted(async () => {
  try {
    await loadSources()
    products.value = (await api.get('/datasource-products')).data
    drivers.value = (await api.get('/settings/database-drivers')).data
  } catch (cause: any) { error.value = cause.response?.data?.detail || '无法加载数据源配置' }
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>数据源</h1>
        <p>配置数据库连接，测试连通性并读取表结构。</p>
      </div>
      <div class="dialog-actions"><t-button v-if="hasRole('admin')" variant="outline" @click="showDrivers = true; connectionMessage = ''">驱动管理</t-button><t-button theme="primary" @click="addSource">添加数据源</t-button></div>
    </div>

    <t-alert v-if="error" theme="error" title="数据源操作失败" :message="error" closable @close="error = ''" />

    <label>扫描范围（留空扫描可见对象；多个表用逗号分隔，如 public.orders）<t-input v-model="scanScope" /></label>
    <t-alert v-if="activeJob" :message="`扫描任务：${activeJob.status} · ${activeJob.phase} · 已完成 ${activeJob.completed_tables || 0} 个对象`">
      <template #operation><t-button v-if="activeJob.status === 'running'" variant="text" @click="cancelScan">取消扫描</t-button></template>
    </t-alert>
    <div class="panel source-list">
      <div class="source" v-for="source in sources" :key="source.id">
        <div class="db-icon"><DataBaseIcon size="26px" /></div>
        <div class="source-main">
          <strong>{{ source.name }}</strong>
          <small>{{ source.type }} · {{ source.objects }} 个对象</small>
          <small v-if="source.last_scan_at">最近扫描 {{ new Date(source.last_scan_at).toLocaleString() }}</small>
        </div>
        <t-tag :theme="statusTheme(source.status) as any" variant="light">
          <i class="status-dot"></i>{{ source.status }} / {{ source.access }}
        </t-tag>
        <div class="capabilities"><t-tag v-for="(label,key) in capabilityLabels" :key="key" size="small" variant="light">{{ label }}：{{ stateLabels[source.capabilities?.[key] || 'pending'] || source.capabilities?.[key] }}</t-tag></div>
        <div class="source-actions">
        <t-button variant="text" @click="viewSnapshot(source)">结构与导出</t-button>
        <t-button variant="text" @click="validateQuery(source)">验证查询</t-button>
        <t-button variant="text" @click="editSource(source)">编辑</t-button>
        <t-button variant="text" :loading="loadingId === `test:${source.id}`" @click="testSource(source)">
          <template #icon><LinkIcon /></template>连接测试
        </t-button>
        <t-button variant="outline" :disabled="activeJob?.status === 'running'" :loading="loadingId === `scan:${source.id}`" @click="doScan(source)">
          <template #icon><RefreshIcon /></template>扫描 Schema
        </t-button>
        </div>
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
          {{ scan.connection?.account_read_only === false ? '账号有写权限 · 执行强制只读' : scan.connection?.read_only ? '只读验证通过' : '只读边界待验证' }}
        </t-tag>
      </div>

      <div class="export-actions"><t-button @click="downloadMdl">导出 Wren MDL 与报告</t-button>
        <span v-if="scan.connection">{{ scan.connection.product }} {{ scan.connection.version }} · 驱动 {{ scan.connection.driver_version }} · 模式 {{ scan.connection.compatibility_mode || '自动' }}</span>
      </div>
      <t-alert v-if="scan.permission_gaps?.length" theme="warning" :message="`有 ${scan.permission_gaps.length} 项元数据未能完整读取，详见导出报告。`" />
      <pre v-if="scan.mdl_report" class="report">{{ JSON.stringify(scan.mdl_report, null, 2) }}</pre>
      <div v-for="relation in scan.relationship_candidates || []" :key="relation.id" class="relation-row">
        <span>{{ relation.source_table }}({{ relation.columns.join(', ') }}) → {{ relation.target_table }}({{ relation.target_columns.join(', ') }}) · {{ relation.evidence.join(' / ') }} · {{ relation.status }}</span>
        <t-button size="small" variant="text" @click="openRelation(relation)">提交本体候选</t-button><t-button size="small" variant="text" @click="profileRelation(relation)">启用采样验证（每表 200 行）</t-button><small v-if="relation.profile">{{ JSON.stringify(relation.profile) }}</small><t-button size="small" variant="outline" @click="decideRelation(relation,'accepted')">确认</t-button>
        <t-button size="small" variant="text" @click="decideRelation(relation,'rejected')">否决</t-button>
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

    <t-dialog top="6vh" v-model:visible="showAdd" :header="editingId ? '编辑数据源' : '添加数据源'" :footer="false" width="560px">
      <div class="source-form">
        <label>数据源名称<t-input v-model="form.name" placeholder="例如：业务分析只读库" /></label>
        <label>数据库类型
          <t-select v-model="form.type" :disabled="!!editingId" @change="selectProduct">
            <t-option v-for="product in products" :key="product.id" :value="product.id" :label="product.label" />
          </t-select>
        </label>
        <template v-if="!['mysql','postgresql'].includes(form.type)">
          <label>JDBC 驱动<t-select v-model="form.driver_id" clearable><t-option v-for="driver in drivers.filter(d => d.enabled)" :key="driver.id" :value="driver.id" :label="`${driver.name} ${driver.version}`" /></t-select></label>
          <label v-if="form.type === 'oceanbase'">租户模式<t-select v-model="form.compatibility_mode"><t-option value="mysql" label="MySQL 模式" /><t-option value="oracle" label="Oracle 模式（OceanBase 协议）" /></t-select></label>
          <label v-if="form.type === 'oracle'">连接方式<t-select v-model="form.service_kind"><t-option value="service" label="Service Name" /><t-option value="sid" label="SID" /></t-select></label>
          <small>不限版本配置；实际能力通过连接、扫描和查询分别验证。未配置方言及只读策略时仅开放扫描。</small>
        </template>
        <label>数据库地址<t-input v-model="form.host" placeholder="IP 地址或域名，不含协议和端口" /></label>
        <label>端口<t-input-number v-model="form.port" :min="1" :max="65535" /></label>
        <label>数据库名<t-input v-model="form.database" placeholder="填写数据库名" /></label>
        <label>用户名<t-input v-model="form.username" autocomplete="off" /></label>
        <label>密码<t-input v-model="form.password" type="password" autocomplete="new-password" :placeholder="passwordConfigured ? '已配置，留空保留原密码' : '填写数据库密码'" /></label>
        <details class="advanced"><summary>高级连接与扫描范围（可选）</summary>
          <label v-if="!['mysql','postgresql'].includes(form.type)">高级 JDBC 连接串（可选，不含账号密码）<t-input v-model="form.jdbc_url" placeholder="jdbc:厂商协议://主机:端口/数据库" /></label>
          <label v-if="!['mysql','postgresql'].includes(form.type)">产品识别纠正（保留自动识别结果）<t-input v-model="form.product_override" /></label>
          <label>Schema<t-input v-model="form.schema_name" placeholder="留空使用可见范围；MySQL 使用数据库名" /></label>
          <label>Catalog<t-input v-model="form.catalog" placeholder="原生连接留空或填写数据库名" /></label>
          <label>连接属性 JSON（TLS 等，MySQL 支持 ssl_ca / ssl_verify_cert 等）<t-textarea v-model="advancedProperties" /></label>
        </details>
        <small v-if="editingId">保存保留数据源与本体的绑定。更换地址或数据库后，请重新扫描并核对原字段映射。</small>
        <t-alert v-if="connectionMessage" :message="connectionMessage" />
        <div class="dialog-actions">
          <t-button variant="outline" @click="showAdd = false">取消</t-button>
          <t-button variant="outline" :loading="creating" :disabled="(!form.jdbc_url && (!form.host || !form.database)) || !form.username" @click="testForm">测试连接</t-button>
          <t-button theme="primary" :loading="creating" :disabled="!form.name || (!form.jdbc_url && (!form.host || !form.database)) || !form.username || (!passwordConfigured && !form.password)" @click="createSource">保存</t-button>
        </div>
      </div>
    </t-dialog>
    <t-dialog top="6vh" v-model:visible="showRelation" header="提交到本体关系维护" :footer="false" width="600px">
      <div class="source-form">
        <label>业务域<t-select v-model="relationForm.domain" @change="relationDomainChanged"><t-option v-for="domain in relationDomains" :key="domain.domain" :value="domain.domain" :label="domain.name || domain.domain" /></t-select></label>
        <label>来源对象<t-select v-model="relationForm.source_object"><t-option v-for="object in relationObjects.filter(o => o.dataSourceId === selected?.id && o.table === relationCandidate?.source_table)" :key="object.name" :value="object.name" :label="object.label || object.name" /></t-select></label>
        <label>目标对象<t-select v-model="relationForm.target_object"><t-option v-for="object in relationObjects.filter(o => o.dataSourceId === selected?.id && o.table === relationCandidate?.target_table)" :key="object.name" :value="object.name" :label="object.label || object.name" /></t-select></label>
        <small>仅使用已经维护的表和字段映射。没有可选对象时，请先到本体维护建立对象；提交后仍需在现有本体候选中确认。</small>
        <t-alert v-if="relationMessage" :message="relationMessage" />
        <t-button :disabled="!relationForm.domain || !relationForm.source_object || !relationForm.target_object" @click="sendRelationCandidate">提交候选</t-button>
      </div>
    </t-dialog>
    <t-dialog top="6vh" v-model:visible="showDrivers" header="管理员 JDBC 驱动与高级适配" :footer="false" width="760px">
      <div class="source-form">
        <div v-for="driver in drivers" :key="driver.id" class="relation-row"><span>{{ driver.name }} · {{ driver.version }} · {{ driver.enabled ? '已启用' : '已停用 / 未安装' }}<small>{{ driver.files?.map((f: any) => f.sha256).join(' / ') }}</small></span><t-button v-if="!driver.builtin" size="small" @click="editDriver(driver)">高级适配</t-button><t-button v-if="!driver.builtin" size="small" @click="toggleDriver(driver)">{{ driver.enabled ? '停用' : '启用' }}</t-button></div>
        <label>主驱动及配套依赖 JAR<input type="file" accept=".jar" multiple @change="driverFiles = Array.from(($event.target as HTMLInputElement).files || [])" /></label>
        <label>驱动与适配配置 JSON<t-textarea v-model="driverConfig" :autosize="{ minRows: 10, maxRows: 18 }" /></label>
        <small>driver_class 填厂商驱动类；dialect 为 mysql / postgres / oracle / tsql；readonly_strategy 为 mysql / postgres / oracle / sqlserver_permissions。pagination 默认 baseline（Oracle 使用 ROWNUM，其他方言使用常规 LIMIT/TOP）；无法确定方言和只读策略时留空，仅扫描。类型映射和元数据模板契约见项目接入文档。</small>
        <t-alert v-if="connectionMessage" :message="connectionMessage" /><div class="dialog-actions"><t-button v-if="editingDriverId" variant="text" @click="editingDriverId = ''">切换为新包上传</t-button><t-button @click="installDriver">{{ editingDriverId ? '保存高级适配' : '上传驱动包' }}</t-button></div>
      </div>
    </t-dialog>
  </div>
</template>

<style scoped>
.capabilities { display:flex; flex-wrap:wrap; gap:5px; grid-column:2 / -1; }.source-actions { grid-column:2 / -1; display:flex; flex-wrap:wrap; gap:8px; }.advanced summary { cursor:pointer; color:var(--semora-muted); }.advanced label { margin-top:14px; }.export-actions,.relation-row { padding:12px 18px; display:flex; align-items:center; gap:10px; }.relation-row span { flex:1; overflow-wrap:anywhere; }.relation-row small { display:block; }.report { padding:16px; max-height:320px; overflow:auto; }.source { flex-wrap:wrap; }
.source-list { margin-top: 18px; }
.source { display: grid; grid-template-columns:50px minmax(180px,1fr) auto; align-items:center; gap:14px; padding: 19px 20px; border-bottom: 1px solid var(--semora-line); }
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
.source-form { display: flex; flex-direction: column; gap: 18px; padding: 5px 8px 8px 0; max-height:68vh; overflow-y:auto; }
.source-form label { display: flex; flex-direction: column; gap: 8px; font-weight: 600; }.source-form small { color: var(--semora-muted); font-weight: 400; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 5px; }
@media (max-width: 1280px) { .schema-grid { grid-template-columns: 1fr; } }
</style>
