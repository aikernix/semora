<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RelationIcon } from 'tdesign-icons-vue-next'
import { api } from '../api'

const drafts = ref<any[]>([])
const selectedDomain = ref('')
const draft = ref<any>(null)
const candidates = ref<any[]>([])
const publishing = ref(false)
const saving = ref(false)
const published = ref<any>(null)
const publication = ref<any>(null)
const error = ref('')
const reviewMessage = ref('')
const editorVisible = ref(false)
const editorText = ref('')
const dataSources = ref<any[]>([])
const mappingsDirty = ref(false)

const pendingCount = computed(() => candidates.value.filter(item => item.status === 'pending_review').length)

async function loadDomains() {
  drafts.value = (await api.get('/ontology/drafts')).data
  const sources = (await api.get('/datasources')).data
  dataSources.value = Array.isArray(sources) ? sources : []
  if (!selectedDomain.value && drafts.value.length) selectedDomain.value = drafts.value[0].domain
}

async function loadDomain() {
  if (!selectedDomain.value) return
  error.value = ''
  publication.value = null
  try {
    const [draftResponse, candidateResponse, publicationResponse] = await Promise.all([
      api.get(`/ontology/drafts/${selectedDomain.value}`),
      api.get('/ontology/candidates', { params: { domain: selectedDomain.value } }),
      api.get('/ontology/publication-status', { params: { domain: selectedDomain.value } }),
    ])
    draft.value = draftResponse.data
    mappingsDirty.value = false
    candidates.value = candidateResponse.data
    publication.value = publicationResponse.data
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || 'Ontology Core 不可用'
  }
}

async function load() {
  await loadDomains()
  await loadDomain()
}

async function publish() {
  if (!selectedDomain.value) return
  publishing.value = true
  error.value = ''
  try {
    published.value = (await api.post('/ontology/publish', null, { params: { domain: selectedDomain.value } })).data
    await load()
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || '发布失败'
  } finally {
    publishing.value = false
  }
}

async function decide(item: any, decision: 'accepted' | 'rejected') {
  const note = window.prompt('请输入审核意见', decision === 'accepted' ? '证据与结构化映射已核对' : '证据不足或口径不适用')
  if (note === null) return
  try {
    const response = await api.post(`/ontology/candidates/${item.id}/decision`, { decision, note })
    reviewMessage.value = response.data.kind === 'definition' || decision === 'rejected'
      ? `候选已${decision === 'accepted' ? '接受' : '拒绝'}。`
      : '候选已合并到草稿，草稿版本已自动递增；发布前仍需完整校验。'
    await load()
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || '候选审核失败'
  }
}

function openEditor() {
  if (!draft.value) return
  editorText.value = JSON.stringify(draft.value, null, 2)
  editorVisible.value = true
}

function createDomain() {
  const domain = window.prompt('请输入业务域标识（小写字母、数字、下划线）')?.trim()
  if (!domain) return
  if (!/^[a-z][a-z0-9_]{1,99}$/.test(domain)) {
    error.value = '业务域标识格式不正确'
    return
  }
  const name = window.prompt('请输入业务域名称', domain)?.trim() || domain
  selectedDomain.value = domain
  draft.value = { domain, name, version: '1.0.0', description: '', dataSourceId: '', knowledgeBaseIds: [], scopeField: '', timeField: '', defaultFilters: [], objects: [], relations: [], metrics: [] }
  candidates.value = []
  openEditor()
}

async function saveDraft() {
  let value: any
  try {
    value = JSON.parse(editorText.value)
  } catch {
    error.value = '草稿不是有效 JSON'
    return
  }
  if (value.domain !== selectedDomain.value) {
    error.value = '业务域标识不能在编辑过程中修改'
    return
  }
  saving.value = true
  error.value = ''
  try {
    await api.put(`/ontology/drafts/${selectedDomain.value}`, value)
    editorVisible.value = false
    reviewMessage.value = '本体草稿已通过完整性校验并保存。'
    await load()
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || '草稿校验或保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(load)

function sourceOf(name: string) { return draft.value.objects.find((o: any) => o.name === name)?.dataSourceId || draft.value.dataSourceId }
async function saveMappings() {
  const value = JSON.parse(JSON.stringify(draft.value))
  if (publication.value?.published_version === value.version) {
    const parts = value.version.split('.')
    const last = Number(parts[parts.length - 1])
    value.version = Number.isFinite(last) ? [...parts.slice(0, -1), String(last + 1)].join('.') : `${value.version}.1`
  }
  saving.value = true
  try {
    await api.put(`/ontology/drafts/${selectedDomain.value}`, value)
    reviewMessage.value = '数据源映射已保存为草稿，发布后供新查询使用。跨源关系将用于分步传递关联键。'
    await load()
  } catch (reason: any) { error.value = reason?.response?.data?.detail || '映射保存失败' }
  finally { saving.value = false }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div><h1>本体工作室</h1><p>按业务域管理对象、属性、关系、指标、证据候选和不可变发布快照。</p></div>
      <div class="header-actions">
        <t-select v-model="selectedDomain" style="width:240px" @change="loadDomain">
          <t-option v-for="item in drafts" :key="item.domain" :value="item.domain" :label="`${item.name} · ${item.version}`" />
        </t-select>
        <t-button variant="outline" @click="createDomain">新建业务域</t-button>
        <t-button variant="outline" :disabled="!draft" @click="openEditor">编辑草稿</t-button>
        <t-button theme="primary" :loading="publishing" :disabled="!draft || mappingsDirty" @click="publish">发布当前草稿</t-button>
      </div>
    </div>
    <t-alert v-if="error" theme="error" :message="error" style="margin-bottom:16px" />
    <t-alert v-if="reviewMessage" theme="success" title="操作完成" :message="reviewMessage" style="margin-bottom:16px" />

    <template v-if="draft">
      <t-alert v-if="publication" :theme="publication.published_version === draft.version ? 'info' : 'warning'" style="margin-bottom:16px" :title="publication.published_version ? `新查询使用已发布本体 ${publication.published_version}` : '当前业务域尚未发布本体'">
        <template #message>已发布：{{ publication.published_counts.objects }} 个对象、{{ publication.published_counts.relations }} 条关系、{{ publication.published_counts.metrics }} 个指标。当前编辑的是草稿 {{ draft.version }}；保存草稿不会影响查询，需点击“发布当前草稿”后供新查询使用。已有查询继续绑定原版本。</template>
      </t-alert>
      <div class="domain-summary panel">
        <div><strong>{{ draft.name }}</strong><span>{{ draft.domain }} · 草稿 {{ draft.version }}</span></div>
        <p>{{ draft.description || '暂无业务域说明' }} · 数据源 {{ draft.dataSourceId || '未绑定' }} · 知识库 {{ draft.knowledgeBaseIds?.length || 0 }} 个</p>
        <div class="ontology-stats">
          <span>对象类 <b>{{ draft.objects?.length || 0 }}</b></span><span>关系类 <b>{{ draft.relations?.length || 0 }}</b></span>
          <span>指标 <b>{{ draft.metrics?.length || 0 }}</b></span><span>待审候选 <b>{{ pendingCount }}</b></span>
        </div>
      </div>
      <div class="panel" style="padding:16px;margin-bottom:16px">
        <label>默认数据源 <t-select v-model="draft.dataSourceId" @change="mappingsDirty = true" style="width:300px;display:inline-block;margin:0 12px"><t-option v-for="source in dataSources" :key="source.id" :value="source.id" :label="`${source.name} · ${source.type}`" /></t-select></label>
        <t-button :disabled="!mappingsDirty" :loading="saving" @click="saveMappings">保存数据源映射</t-button>
        <p>对象可绑定不同实例。跨源关系只定义关联键，由程序分步查询，不生成跨实例 JOIN。机构权限映射可在草稿 sourceScopes 中按数据源配置。</p>
      </div>

      <div class="ontology-grid">
        <section class="model-column">
          <div class="panel model-panel">
            <div class="panel-title">对象与属性映射</div>
            <div v-if="draft.objects?.length" class="object-grid">
              <article v-for="object in draft.objects" :key="object.name" class="object-card">
                <div><strong>{{ object.label }}</strong><code>{{ object.name }}</code></div>
                <small>{{ object.table }} · PK {{ object.primaryKey }}</small>
                <t-select v-model="object.dataSourceId" placeholder="沿用默认数据源" clearable @change="mappingsDirty = true" style="margin:10px 0"><t-option v-for="source in dataSources" :key="source.id" :value="source.id" :label="`${source.name} · ${source.type}`" /></t-select>
                <div class="property-list"><span v-for="property in object.properties" :key="property.name" :class="{ sensitive: property.sensitive }">{{ property.label || property.name }} → {{ property.column }}</span></div>
              </article>
            </div>
            <div v-else class="empty-hint">尚未定义对象；请编辑草稿并完成物理表映射。</div>
          </div>

          <div class="panel model-panel">
            <div class="panel-title">关系路径</div>
            <div v-if="draft.relations?.length" class="relation-list">
              <article v-for="relation in draft.relations" :key="relation.name"><RelationIcon /><div><strong>{{ relation.label || relation.name }}</strong><span>{{ relation.from }} → {{ relation.to }} · {{ relation.cardinality }} · {{ sourceOf(relation.from) === sourceOf(relation.to) ? '同源关联' : '跨源分步查询' }}</span><code>{{ relation.condition }}</code></div></article>
            </div>
            <div v-else class="empty-hint">尚未定义对象关系。</div>
          </div>

          <div class="panel model-panel">
            <div class="panel-title">指标目录</div>
            <div v-if="draft.metrics?.length" class="metric-table">
              <div class="metric-row metric-head"><span>指标</span><span>语义表达式</span><span>粒度</span><span>格式</span></div>
              <div v-for="metric in draft.metrics" :key="metric.name" class="metric-row"><span><strong>{{ metric.label }}</strong><small>{{ metric.name }}</small></span><code>{{ metric.expression }}</code><span>{{ metric.grain }}</span><span>{{ metric.format || '-' }}</span></div>
            </div>
            <div v-else class="empty-hint">尚未定义指标。</div>
          </div>
        </section>

        <aside class="panel candidate-panel">
          <div class="panel-title">知识抽取候选 <t-tag>{{ pendingCount }}</t-tag></div>
          <article v-for="item in candidates" :key="item.id" class="candidate">
            <RelationIcon /><div><strong>{{ item.term }}</strong> <t-tag size="small" variant="outline">{{ item.kind || 'definition' }}</t-tag><small>{{ item.source }}</small><p>{{ item.proposal }}</p>
              <details v-if="item.evidence?.excerpt"><summary>查看原文证据</summary><p>{{ item.evidence.excerpt }}</p></details>
              <small v-if="item.evidence?.locator">证据：{{ item.evidence.locator }} · {{ item.evidence.revision }}</small><small v-if="item.reviewedBy">审核：{{ item.reviewedBy }} · {{ item.reviewNote }}</small>
              <t-tag v-if="item.status !== 'pending_review'" :theme="item.status === 'accepted' ? 'success' : 'danger'">{{ item.status }}</t-tag>
              <div v-else><t-button size="small" variant="outline" @click="decide(item, 'rejected')">拒绝</t-button><t-button size="small" theme="primary" @click="decide(item, 'accepted')">接受候选</t-button></div>
            </div>
          </article>
          <div v-if="!candidates.length" class="empty-hint">该业务域暂无知识候选。</div>
        </aside>
      </div>
    </template>
    <div v-else class="panel empty-hint">暂无本体草稿，请新建业务域。</div>

    <t-alert v-if="published" theme="success" title="本体发布成功" :message="`${published.domain} · ${published.version} · ${published.artifactVersion} · ${published.contentHash}`" style="margin-top:16px" />
    <t-dialog v-model:visible="editorVisible" header="编辑本体草稿 JSON" width="min(1000px, 92vw)" :confirm-btn="{ content: '校验并保存', loading: saving }" @confirm="saveDraft">
      <p class="editor-tip">对象、属性、关系和指标会在服务端执行完整投影校验；校验失败不会覆盖现有草稿。</p><textarea v-model="editorText" class="ontology-editor" spellcheck="false" />
    </t-dialog>
  </div>
</template>

<style scoped>
.header-actions{display:flex;gap:10px;align-items:center}.domain-summary{display:grid;grid-template-columns:auto 1fr;align-items:center;margin-bottom:16px;overflow:hidden}.domain-summary>div:first-child{padding:18px}.domain-summary strong,.domain-summary span{display:block}.domain-summary span,.domain-summary p{color:var(--semora-muted);margin-top:5px}.domain-summary>p{padding:0 18px}.ontology-stats{grid-column:1/-1;display:flex;border-top:1px solid var(--semora-line)}.ontology-stats span{flex:1;padding:14px 16px;text-align:center;color:var(--semora-muted)}.ontology-stats b{color:var(--semora-ink);margin-left:8px}.ontology-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.model-column{display:grid;gap:16px}.model-panel{overflow:hidden}.object-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:16px}.object-card{border:1px solid var(--semora-line);border-radius:10px;padding:15px;min-width:0}.object-card>div:first-child{display:flex;justify-content:space-between;gap:10px}.object-card small,.metric-row small{display:block;color:var(--semora-muted);margin-top:6px}.property-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:13px}.property-list span{font-size:11px;background:var(--semora-page);border-radius:6px;padding:5px 7px}.property-list span.sensitive{background:#fff2e8;color:#9b4c0f}.relation-list article{display:flex;gap:11px;padding:14px 17px;border-top:1px solid var(--semora-line)}.relation-list article:first-child{border-top:0}.relation-list svg{color:var(--semora-green);flex:none}.relation-list span,.relation-list code{display:block;margin-top:5px;color:var(--semora-muted);font-size:12px}.metric-table{overflow:auto}.metric-row{display:grid;grid-template-columns:1.1fr 2.5fr .7fr .7fr;gap:12px;align-items:center;padding:12px 16px;border-top:1px solid var(--semora-line);font-size:12px}.metric-head{color:var(--semora-muted);background:var(--semora-sidebar)}.metric-row code{white-space:normal;overflow-wrap:anywhere}.candidate-panel{overflow:hidden;height:max-content}.candidate{display:flex;gap:10px;padding:17px;border-bottom:1px solid var(--semora-line)}.candidate>svg{color:var(--semora-green);flex:none}.candidate small{display:block;color:var(--semora-muted);margin-top:5px}.candidate p{line-height:1.6}.candidate .t-button{margin-right:7px}.candidate details{margin:8px 0;color:var(--semora-muted);font-size:12px}.candidate summary{cursor:pointer}.candidate details p{max-height:120px;overflow:auto}.editor-tip{color:var(--semora-muted)}.ontology-editor{width:100%;height:58vh;resize:vertical;border:1px solid var(--semora-line);border-radius:8px;padding:14px;background:#102018;color:#d9f8e4;font:12px/1.6 Consolas,monospace;outline:0}.empty-hint{padding:30px;text-align:center;color:var(--semora-muted)}
@media(max-width:1100px){.ontology-grid{grid-template-columns:1fr}.object-grid{grid-template-columns:1fr}.header-actions{flex-wrap:wrap}.metric-row{grid-template-columns:1fr}.metric-head{display:none}}
</style>
