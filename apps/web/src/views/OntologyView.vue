<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RelationIcon } from 'tdesign-icons-vue-next'
import { api } from '../api'

const domains = ref<any[]>([])
const selectedDomain = ref('')
const ontology = ref<any>(null)
const dataSources = ref<any[]>([])
const sourcePackages = ref<Record<string, any>>({})
const crossLeft = ref('')
const crossRight = ref('')
const crossJob = ref<any>(null)
let crossTimer: ReturnType<typeof setTimeout> | undefined
const saving = ref(false)
const error = ref('')
const message = ref('')
const relationVisible = ref(false)
const relationIndex = ref(-1)
const relationDraft = ref<any>({})
const relationError = ref('')
const deletingRelation = ref<any>(null)
const createVisible = ref(false)
const creating = ref(false)
const createError = ref('')
const newDomain = ref({ name: '', domain: '' })
let loadVersion = 0

function mapped(value: any) {
  return { ...value, relations: value.relations || [], objects: (value.objects || []).map((object: any) => ({
    ...object, dataSourceId: object.dataSourceId || value.dataSourceId || '',
  })) }
}
function editable(value: any) {
  const { knowledgeBaseIds, version, dataSourceId, ...configuration } = value
  return configuration
}
async function loadDomains() {
  const [items, sources] = await Promise.all([api.get('/ontology/configurations'), api.get('/datasources')])
  domains.value = items.data
  dataSources.value = sources.data
  const packages: Record<string, any> = {}
  await Promise.all(dataSources.value.map(async source => {
    try { packages[source.id] = (await api.get(`/datasources/${source.id}/source-ontology`)).data }
    catch (reason: any) { if (reason.response?.status !== 404) throw reason }
  }))
  sourcePackages.value = packages
  if (!domains.value.some(item => item.domain === selectedDomain.value)) selectedDomain.value = domains.value[0]?.domain || ''
}
const boundSourceIds = computed(() => new Set([
  ...(ontology.value?.sourcePackageRefs || []).map((item: any) => item.dataSourceId),
  ...(ontology.value?.objects || []).map((item: any) => item.dataSourceId || ontology.value?.dataSourceId),
].filter(Boolean)))
const crossSources = computed(() => dataSources.value.filter(source => boundSourceIds.value.has(source.id) && ['ready','ready_with_pending'].includes(sourcePackages.value[source.id]?.status)))
async function bindSource(sourceId: string) {
  saving.value = true; error.value = ''
  try { ontology.value = mapped((await api.post(`/ontology/configurations/${selectedDomain.value}/sources/${sourceId}`)).data); message.value = '数据源本体包已绑定。' }
  catch (reason: any) { error.value = reason.response?.data?.detail || '绑定数据源失败' }
  finally { saving.value = false }
}
async function pollCross() {
  if (!crossJob.value) return
  try {
    crossJob.value = (await api.get(`/ontology/configurations/${selectedDomain.value}/cross-source-analysis/${crossJob.value.id}`)).data
    if (!['completed','completed_with_pending','failed'].includes(crossJob.value.status)) crossTimer = setTimeout(() => { void pollCross() }, 1500)
    else { await loadDomain(); message.value = crossJob.value.status === 'failed' ? '' : '跨源关系分析已完成。'; if (crossJob.value.error) error.value = crossJob.value.error }
  } catch (reason: any) { error.value = reason.response?.data?.detail || '无法读取跨源分析状态' }
}
async function analyzeCrossSource() {
  error.value = ''
  try { crossJob.value = (await api.post(`/ontology/configurations/${selectedDomain.value}/cross-source-analysis`, { left_source_id: crossLeft.value, right_source_id: crossRight.value })).data; await pollCross() }
  catch (reason: any) { error.value = reason.response?.data?.detail || '无法启动跨源分析' }
}
async function decideCrossCandidate(candidate: any, status: string) {
  try {
    const updated = (await api.put(`/relationship-candidates/${candidate.id}`, { status, note: '' })).data
    Object.assign(candidate, updated)
    if (status === 'accepted') await loadDomain()
  } catch (reason: any) { error.value = reason.response?.data?.detail || '无法更新候选关系' }
}
async function loadDomain() {
  const domain = selectedDomain.value
  const version = ++loadVersion
  ontology.value = null
  relationVisible.value = false
  deletingRelation.value = null
  message.value = ''
  error.value = ''
  if (!domain) return
  try {
    const result = (await api.get(`/ontology/configurations/${domain}`)).data
    if (version === loadVersion && domain === selectedDomain.value) ontology.value = mapped(result)
  } catch (reason: any) { error.value = reason.response?.data?.detail || '无法读取本体配置' }
}
async function load() {
  try { await loadDomains(); await loadDomain() }
  catch (reason: any) { error.value = reason.response?.data?.detail || '无法读取业务域' }
}
function openCreate() {
  newDomain.value = { name: '', domain: '' }
  createError.value = ''
  createVisible.value = true
}
async function createDomain() {
  const domain = newDomain.value.domain.trim()
  const name = newDomain.value.name.trim()
  if (!name || !/^[a-z][a-z0-9_]{1,99}$/.test(domain)) {
    createError.value = '请填写业务域名称；标识为2～100位小写字母、数字或下划线，并以字母开头。'
    return
  }
  creating.value = true
  createError.value = ''
  try {
    await api.post('/ontology/domains', { name, domain })
    selectedDomain.value = domain
    createVisible.value = false
    await load()
    message.value = '业务域已创建，请配置对象与关联关系。'
  } catch (reason: any) { createError.value = reason.response?.data?.detail || '创建业务域失败' }
  finally { creating.value = false }
}
function objectOf(name: string) { return ontology.value?.objects.find((object: any) => object.name === name) }
function tableLabel(name: string) {
  const object = objectOf(name)
  return object ? `${object.label || object.name} · ${object.table}` : name
}
function sourceOf(name: string) { return objectOf(name)?.dataSourceId || '' }
function sourceLabel(name: string) {
  return dataSources.value.find(source => source.id === sourceOf(name))?.name || sourceOf(name) || '未配置数据源'
}
function openRelation(index = -1) {
  relationIndex.value = index
  relationDraft.value = index < 0
    ? { name: '', label: '', from: '', to: '', cardinality: 'N:1', joinType: 'many_to_one', condition: '', optional: false }
    : JSON.parse(JSON.stringify(ontology.value.relations[index]))
  relationError.value = ''
  relationVisible.value = true
}
async function saveConfiguration(value: any) {
  const domain = selectedDomain.value
  if (!value || value.domain !== domain) throw new Error('业务域标识不能在编辑过程中更改')
  const result = (await api.put(`/ontology/configurations/${domain}`, value)).data
  if (domain === selectedDomain.value) {
    ontology.value = mapped(result)
  }
  await loadDomains()
  message.value = '本体配置已保存，新查询立即使用；已开始的查询保留原配置。'
}
async function saveRelation() {
  const relation = { ...relationDraft.value, name: relationDraft.value.name.trim(), condition: relationDraft.value.condition.trim() }
  if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(relation.name) || !relation.from || !relation.to || !relation.condition) {
    relationError.value = '请填写关系标识、两端表及关联条件；标识仅支持字母、数字和下划线，且不能以数字开头。'
    return
  }
  if (ontology.value.relations.some((item: any, index: number) => index !== relationIndex.value && item.name === relation.name)) {
    relationError.value = '关系标识已存在，请使用其他标识。'
    return
  }
  const cardinalities: Record<string, string> = { '1:1': 'one_to_one', '1:N': 'one_to_many', 'N:1': 'many_to_one', 'N:N': 'many_to_many' }
  if (relationIndex.value < 0 || relation.cardinality !== ontology.value.relations[relationIndex.value].cardinality) {
    relation.joinType = cardinalities[relation.cardinality]
  }
  const relations = [...ontology.value.relations]
  if (relationIndex.value < 0) relations.push(relation)
  else relations[relationIndex.value] = relation
  saving.value = true
  relationError.value = ''
  try {
    await saveConfiguration({ ...editable(ontology.value), relations })
    relationVisible.value = false
  } catch (reason: any) { relationError.value = reason.response?.data?.detail || reason.message || '关系保存失败' }
  finally { saving.value = false }
}
async function deleteRelation() {
  if (!deletingRelation.value) return
  saving.value = true
  error.value = ''
  try {
    const relations = ontology.value.relations.filter((item: any) => item.name !== deletingRelation.value.name)
    await saveConfiguration({ ...editable(ontology.value), relations })
    deletingRelation.value = null
  } catch (reason: any) { error.value = reason.response?.data?.detail || reason.message || '关系删除失败' }
  finally { saving.value = false }
}
onMounted(load)
onUnmounted(() => { if (crossTimer) clearTimeout(crossTimer) })
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div><h1>本体工作室</h1><p>查看和维护表之间的关联关系，保存后供新查询使用。</p></div>
      <div class="header-actions">
        <t-select v-model="selectedDomain" :disabled="saving" style="width:240px" @change="loadDomain"><t-option v-for="item in domains" :key="item.domain" :value="item.domain" :label="item.name" /></t-select>
        <t-button variant="outline" :disabled="saving" @click="openCreate">新建业务域</t-button>
        <t-button theme="primary" :disabled="!ontology?.objects.length || saving" @click="openRelation()">新增关联</t-button>
      </div>
    </div>
    <t-alert v-if="error" theme="error" :message="error" style="margin-bottom:16px" />
    <t-alert v-if="message" theme="success" :message="message" closable @close="message = ''" style="margin-bottom:16px" />
    <template v-if="ontology">
      <section class="domain-summary panel">
        <div class="domain-heading"><div><strong>{{ ontology.name }}</strong><small>{{ ontology.domain }}</small></div><t-tag variant="light" :theme="ontology.objects.length ? 'success' : 'default'">{{ ontology.objects.length ? '已配置' : '待配置' }}</t-tag></div>
        <div class="ontology-stats"><span>表映射 <b>{{ ontology.objects.length }}</b></span><span>关联关系 <b>{{ ontology.relations.length }}</b></span></div>
      </section>
      <section class="panel source-package-panel">
        <div class="panel-title">数据源本体包</div>
        <div class="package-list">
          <article v-for="source in dataSources" :key="source.id" class="package-row">
            <div><strong>{{ source.name }}</strong><small>{{ source.type }} · {{ sourcePackages[source.id]?.object_count || 0 }} 个对象 · {{ sourcePackages[source.id]?.relation_count || 0 }} 条关系</small></div>
            <t-tag variant="light" :theme="sourcePackages[source.id] ? 'success' : 'default'">{{ sourcePackages[source.id]?.status || '尚未完成建模' }}</t-tag>
            <t-button size="small" variant="outline" :disabled="saving || !sourcePackages[source.id] || boundSourceIds.has(source.id)" @click="bindSource(source.id)">{{ boundSourceIds.has(source.id) ? '已绑定' : '绑定到当前业务域' }}</t-button>
          </article>
        </div>
        <div class="cross-analysis">
          <div><strong>分析两个数据源的关联</strong><small>仅分析当前选择的两个已绑定数据源，不遍历其他数据源。</small></div>
          <t-select v-model="crossLeft" placeholder="选择数据源 A" style="width:210px"><t-option v-for="source in crossSources" :key="source.id" :value="source.id" :label="source.name" /></t-select>
          <t-select v-model="crossRight" placeholder="选择数据源 B" style="width:210px"><t-option v-for="source in crossSources.filter(item => item.id !== crossLeft)" :key="source.id" :value="source.id" :label="source.name" /></t-select>
          <t-button :disabled="!crossLeft || !crossRight || !!crossJob && !['completed','completed_with_pending','failed'].includes(crossJob.status)" @click="analyzeCrossSource">分析关联</t-button>
        </div>
        <t-alert v-if="crossJob" :theme="crossJob.status === 'failed' ? 'error' : 'info'" :message="`跨源分析：${crossJob.phase} · ${crossJob.progress}%${crossJob.details?.accepted !== undefined ? ` · 自动采用 ${crossJob.details.accepted} 条，待确认 ${crossJob.details.pending} 条` : ''}`" />
        <div v-if="crossJob?.candidates?.length" class="cross-candidates">
          <article v-for="candidate in crossJob.candidates" :key="candidate.id">
            <div><strong>{{ candidate.source_table_id }} → {{ candidate.target_table_id }}</strong>
            <small>{{ candidate.source_columns.join(', ') }} ↔ {{ candidate.target_columns.join(', ') }} · {{ candidate.evidence_grade }} · 置信度 {{ candidate.confidence }} · {{ candidate.status }}</small></div>
            <t-button v-if="candidate.status === 'pending'" size="small" variant="outline" @click="decideCrossCandidate(candidate, 'accepted')">采用</t-button>
            <t-button v-if="candidate.status === 'pending'" size="small" variant="text" @click="decideCrossCandidate(candidate, 'rejected')">忽略</t-button>
          </article>
        </div>
      </section>
      <section class="panel model-panel">
        <div class="panel-title">表之间的关联关系</div>
        <div v-if="ontology.relations.length" class="relation-list">
          <article v-for="(relation, index) in ontology.relations" :key="relation.name" class="relation-card">
            <div class="relation-header">
              <div><strong>{{ relation.label || relation.name }}</strong><small>{{ relation.name }}</small></div>
              <div class="relation-actions"><t-button variant="text" :disabled="saving" @click="openRelation(Number(index))">编辑</t-button><t-button variant="text" theme="danger" :disabled="saving" @click="deletingRelation = relation">删除</t-button></div>
            </div>
            <div class="relation-path">
              <div class="table-node"><strong>{{ tableLabel(relation.from) }}</strong><small>{{ sourceLabel(relation.from) }}</small></div>
              <div class="relation-link"><span>{{ relation.cardinality }}</span><RelationIcon /><small>{{ sourceOf(relation.from) && sourceOf(relation.to) ? (sourceOf(relation.from) === sourceOf(relation.to) ? '同源关联' : '跨源关联') : '数据源待配置' }}</small></div>
              <div class="table-node"><strong>{{ tableLabel(relation.to) }}</strong><small>{{ sourceLabel(relation.to) }}</small></div>
            </div>
            <div class="relation-condition"><span>关联条件</span><code>{{ relation.condition }}</code><t-tag v-if="relation.optional" variant="light">可选关联</t-tag></div>
          </article>
        </div>
        <div v-else class="empty-hint"><p>{{ ontology.objects.length ? '尚未配置表关联，可手动新增。' : '当前业务域尚未配置表映射，配置后可维护关联关系。' }}</p><t-button v-if="ontology.objects.length" variant="outline" @click="openRelation()">新增关联</t-button></div>
      </section>
    </template>
    <div v-else class="panel empty-hint">请选择或新建业务域。</div>
    <t-dialog v-model:visible="createVisible" header="新建业务域" :footer="false" width="520px">
      <div class="domain-form">
        <label>业务域名称<t-input v-model="newDomain.name" placeholder="填写业务域名称" :maxlength="200" /></label>
        <label>业务域标识<t-input v-model="newDomain.domain" placeholder="例如：operations_analysis" :maxlength="100" /><small>2～100位小写字母、数字或下划线，以字母开头；创建后不可更改。</small></label>
        <t-alert v-if="createError" theme="error" :message="createError" />
        <div class="dialog-actions"><t-button variant="outline" @click="createVisible = false">取消</t-button><t-button theme="primary" :loading="creating" :disabled="!newDomain.name.trim() || !newDomain.domain.trim()" @click="createDomain">创建业务域</t-button></div>
      </div>
    </t-dialog>
    <t-dialog v-model:visible="relationVisible" :header="relationIndex < 0 ? '新增关联关系' : '编辑关联关系'" width="min(720px, 92vw)" top="6vh" :footer="false" :close-on-overlay-click="!saving" :close-btn="!saving" :close-on-esc-keydown="!saving">
      <div class="relation-form">
        <t-alert v-if="relationError" theme="error" :message="relationError" />
        <div class="form-grid">
          <label>关系名称<t-input v-model="relationDraft.label" placeholder="例如：客户持有贷款" :disabled="saving" /></label>
          <label>关系标识<t-input v-model="relationDraft.name" placeholder="例如：customer_has_loan" :disabled="saving" /></label>
          <label>来源表<t-select v-model="relationDraft.from" :disabled="saving" filterable placeholder="选择来源表"><t-option v-for="object in ontology?.objects || []" :key="object.name" :value="object.name" :label="`${tableLabel(object.name)} · ${sourceLabel(object.name)} (${object.name})`" /></t-select></label>
          <label>目标表<t-select v-model="relationDraft.to" :disabled="saving" filterable placeholder="选择目标表"><t-option v-for="object in ontology?.objects || []" :key="object.name" :value="object.name" :label="`${tableLabel(object.name)} · ${sourceLabel(object.name)} (${object.name})`" /></t-select></label>
          <label>关联基数<t-select v-model="relationDraft.cardinality" :disabled="saving"><t-option value="1:1" label="一对一 (1:1)" /><t-option value="1:N" label="一对多 (1:N)" /><t-option value="N:1" label="多对一 (N:1)" /><t-option value="N:N" label="多对多 (N:N)" /></t-select></label>
          <label>可选关联<t-switch v-model="relationDraft.optional" :disabled="saving" /><small>允许来源表记录没有匹配的目标记录。</small></label>
        </div>
        <label>关联条件<t-textarea v-model="relationDraft.condition" :disabled="saving" :autosize="{ minRows: 3, maxRows: 8 }" placeholder="例如：customer.id = loan.customer_id" /><small>使用对象标识引用字段；复合关联可用 AND 连接。{{ relationDraft.from || '来源对象' }} → {{ relationDraft.to || '目标对象' }}</small></label>
        <div class="dialog-actions"><t-button variant="outline" :disabled="saving" @click="relationVisible = false">取消</t-button><t-button theme="primary" :loading="saving" @click="saveRelation">保存并生效</t-button></div>
      </div>
    </t-dialog>
    <t-dialog :visible="!!deletingRelation" header="删除关联关系" :confirm-btn="{ content: '删除并生效', theme: 'danger', loading: saving }" :cancel-btn="{ disabled: saving }" :close-btn="!saving" :close-on-overlay-click="false" :close-on-esc-keydown="!saving" @confirm="deleteRelation" @cancel="deletingRelation = null" @close="deletingRelation = null">
      <p>确认删除“{{ deletingRelation?.label || deletingRelation?.name }}”？保存后新查询将不再使用该关联。</p>
    </t-dialog>
  </div>
</template>

<style scoped>
.header-actions{display:flex;gap:10px;align-items:center}.domain-summary{margin-bottom:20px;overflow:hidden}.domain-heading{display:flex;justify-content:space-between;align-items:center;padding:20px 22px}.domain-heading strong{font-size:18px}.domain-heading small{display:block;color:var(--semora-muted);margin-top:6px}.ontology-stats{display:flex;border-top:1px solid var(--semora-line)}.ontology-stats span{flex:1;padding:14px 16px;text-align:center;color:var(--semora-muted)}.ontology-stats b{color:var(--semora-ink);margin-left:8px}.source-package-panel{margin-bottom:20px;overflow:hidden}.package-list{display:grid}.package-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:14px;align-items:center;padding:14px 18px;border-top:1px solid var(--semora-line)}.package-row small,.cross-analysis small,.cross-candidates small{display:block;color:var(--semora-muted);margin-top:5px}.cross-analysis{display:flex;gap:12px;align-items:center;padding:18px;border-top:1px solid var(--semora-line);background:var(--semora-page)}.cross-analysis>div{flex:1}.cross-candidates{display:grid;gap:1px;background:var(--semora-line)}.cross-candidates article{display:flex;align-items:center;gap:8px;padding:12px 18px;background:var(--semora-surface)}.cross-candidates article>div{flex:1}.model-panel{overflow:hidden;margin-bottom:20px}.relation-list{display:grid;gap:16px;padding:18px}.relation-card{border:1px solid var(--semora-line);border-radius:10px;min-width:0;padding:18px}.relation-header{display:flex;justify-content:space-between;align-items:center;gap:12px}.relation-header small{display:block;margin-top:5px;color:var(--semora-muted);overflow-wrap:anywhere}.relation-actions{display:flex;flex:none}.relation-path{display:grid;grid-template-columns:minmax(0,1fr) 120px minmax(0,1fr);align-items:center;gap:16px;margin:18px 0}.table-node{background:var(--semora-page);border:1px solid var(--semora-line);border-radius:8px;padding:14px;overflow-wrap:anywhere}.table-node small{display:block;color:var(--semora-muted);margin-top:8px}.relation-link{display:flex;flex-direction:column;align-items:center;gap:6px;color:var(--semora-green)}.relation-link small{color:var(--semora-muted)}.relation-condition{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;font-size:12px}.relation-condition>span{color:var(--semora-muted)}.relation-condition code{overflow-wrap:anywhere;flex:1;min-width:0}.domain-form,.relation-form{display:flex;flex-direction:column;gap:20px;padding-top:6px}.domain-form label,.relation-form label{display:flex;flex-direction:column;gap:8px;min-width:0}.domain-form small,.relation-form small{color:var(--semora-muted);line-height:1.6}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.dialog-actions{display:flex;justify-content:flex-end;gap:10px}.empty-hint{padding:30px;text-align:center;color:var(--semora-muted)}
@media(max-width:800px){.header-actions,.cross-analysis{flex-wrap:wrap}.page-header{flex-wrap:wrap;gap:16px}.package-row{grid-template-columns:1fr}.relation-path{grid-template-columns:1fr}.relation-link svg{transform:rotate(90deg)}.form-grid{grid-template-columns:1fr}.relation-header{align-items:flex-start}}
</style>
