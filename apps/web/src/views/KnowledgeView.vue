<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BookIcon, LinkIcon, RefreshIcon, SearchIcon } from 'tdesign-icons-vue-next'
import { api, type KnowledgeResearchResult } from '../api'

const status = ref<any>({ status: 'loading', provider: 'weknora' })
const bases = ref<any[]>([])
const domains = ref<any[]>([])
const selectedKb = ref('')
const selectedDomain = ref('')
const boundIds = ref<string[]>([])
const query = ref('')
const research = ref<KnowledgeResearchResult | null>(null)
const loading = ref(false)
const testing = ref(false)
const binding = ref(false)
const error = ref('')
const message = ref('')
const bindingLoaded = ref(false)
const selectionChanged = ref(false)
let bindingVersion = 0

const selectedBase = computed(() => bases.value.find(item => item.id === selectedKb.value))
const isBound = computed(() => boundIds.value.includes(selectedKb.value))
const missingBoundIds = computed(() => boundIds.value.filter(id => !bases.value.some(base => base.id === id)))

async function loadDomainBinding() {
  if (!selectedDomain.value) return
  const version = ++bindingVersion
  const domain = selectedDomain.value
  bindingLoaded.value = false
  selectionChanged.value = false
  selectedKb.value = ''
  boundIds.value = []
  try {
    const draft = (await api.get(`/ontology/drafts/${domain}`)).data
    if (version !== bindingVersion || domain !== selectedDomain.value) return
    boundIds.value = draft.knowledgeBaseIds || []
    const bound = bases.value.find(item => boundIds.value.includes(item.id))
    selectedKb.value = bound?.id || ''
    bindingLoaded.value = true
  } catch {
    if (version !== bindingVersion) return
    boundIds.value = []
    error.value = '无法读取业务域绑定，请刷新后重试；尚未修改任何绑定。'
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [statusResponse, baseResponse, domainResponse] = await Promise.all([
      api.get('/knowledge/status'), api.get('/knowledge-bases'), api.get('/domains'),
    ])
    status.value = statusResponse.data
    bases.value = baseResponse.data
    domains.value = domainResponse.data
    if (!domains.value.some(item => item.domain === selectedDomain.value) && domains.value.length) {
      selectedDomain.value = domains.value[0].domain
    }
    await loadDomainBinding()
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || 'WeKnora 连接失败'
  } finally {
    loading.value = false
  }
}

async function bindDomain() {
  if (!selectedDomain.value || !bindingLoaded.value || !selectionChanged.value) return
  if (selectedKb.value && !selectedBase.value) {
    error.value = '所选知识库不在当前账号可访问列表中，请核对连接配置。'
    return
  }
  binding.value = true
  error.value = ''
  message.value = ''
  try {
    const draft = (await api.get(`/ontology/drafts/${selectedDomain.value}`)).data
    draft.knowledgeBaseIds = selectedKb.value ? [selectedKb.value] : []
    await api.put(`/ontology/drafts/${selectedDomain.value}`, draft)
    boundIds.value = draft.knowledgeBaseIds
    selectionChanged.value = false
    message.value = selectedKb.value ? `已将“${selectedBase.value?.name}”绑定到业务域 ${selectedDomain.value}，发布本体后生效。` : '已取消知识库绑定。发布本体后，直接基于需求和本体分析。'
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || '业务域绑定失败'
  } finally {
    binding.value = false
  }
}

async function testAgent() {
  if (!query.value.trim() || !selectedKb.value) return
  testing.value = true
  research.value = null
  error.value = ''
  try {
    research.value = (await api.post<KnowledgeResearchResult>('/knowledge/research', {
      query: query.value,
      knowledge_base_ids: [selectedKb.value],
    }, { timeout: 200_000 })).data
  } catch (reason: any) {
    error.value = reason?.response?.data?.detail || 'Wiki Agent 测试失败'
  } finally {
    testing.value = false
  }
}

function openWeKnora() {
  if (status.value.instance_url) window.open(`${status.value.instance_url}/platform/knowledge-bases`, '_blank', 'noopener')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div><h1>知识库配置</h1><p>Semora 不重复解析文档；知识管理、Wiki 图谱和溯源检索由外部 WeKnora Wiki Researcher 完成。</p></div>
      <div class="header-actions">
        <t-button variant="outline" :loading="loading" @click="load"><RefreshIcon />刷新连接</t-button>
        <t-button theme="primary" :disabled="!status.instance_url" @click="openWeKnora"><LinkIcon />打开 WeKnora</t-button>
      </div>
    </div>

    <t-alert v-if="error" theme="error" :message="error" closable @close="error = ''" style="margin-bottom:14px" />
    <t-alert v-if="message" theme="success" :message="message" closable @close="message = ''" style="margin-bottom:14px" />

    <section class="connection panel">
      <div><small>连接状态</small><strong><i class="status-dot"></i>{{ status.status === 'ready' ? '已连接' : status.status }}</strong></div>
      <div><small>WeKnora 实例</small><code>{{ status.instance_url || '未配置' }}</code></div>
      <div><small>Wiki Agent</small><code>{{ status.agent_id || 'builtin-wiki-researcher' }}</code></div>
      <div><small>API Key</small><strong>{{ status.api_key_configured ? '已安全配置' : '未配置' }}</strong></div>
      <div><small>可访问知识库</small><strong>{{ bases.length }}</strong></div>
    </section>

    <div class="layout">
      <section class="panel config-panel">
        <div class="panel-title">业务域与知识库绑定</div>
        <div class="form">
          <label>业务域
            <t-select v-model="selectedDomain" @change="loadDomainBinding">
              <t-option v-for="domain in domains" :key="domain.domain" :value="domain.domain" :label="`${domain.name} · ${domain.version}`" />
            </t-select>
          </label>
          <label>WeKnora 知识库
            <t-select v-model="selectedKb" :disabled="!bindingLoaded" @change="selectionChanged = true">
              <t-option value="" label="不使用知识库（可选辅助）" />
              <t-option v-for="base in bases" :key="base.id" :value="base.id" :label="`${base.name} · ${base.documents || 0} 篇`" />
            </t-select>
          </label>
          <t-alert v-if="missingBoundIds.length" theme="warning" title="当前连接无法访问已绑定知识库" :message="`原绑定 ID：${missingBoundIds.join('、')}。请检查 WeKnora 账号和 API Key；系统不会自动选择其他知识库。`" />
          <article v-if="selectedBase" class="kb-card">
            <BookIcon size="24px" />
            <div><strong>{{ selectedBase.name }}</strong><small>{{ selectedBase.id }}</small><p>{{ selectedBase.description || '暂无知识库说明' }}</p></div>
            <t-tag :theme="isBound ? 'success' : 'default'" variant="light">{{ isBound ? '当前已绑定' : '尚未绑定' }}</t-tag>
          </article>
          <t-button theme="primary" :loading="binding" :disabled="!selectedDomain || !bindingLoaded || !selectionChanged || isBound" @click="bindDomain">保存知识库绑定</t-button>
          <p class="hint">绑定写入本体草稿；发布本体后，查询运行会锁定该知识库 ID，避免跨业务域误检索。</p>
        </div>
      </section>

      <section class="panel test-panel">
        <div class="panel-title">Wiki Agent ReAct 测试</div>
        <div class="searchbar"><SearchIcon /><input v-model="query" placeholder="输入一个业务口径问题" @keydown.enter="testAgent" /><t-button :loading="testing" :disabled="!selectedKb" @click="testAgent">开始研究</t-button></div>
        <div v-if="research" class="research">
          <div class="answer"><small>Agent 结论</small><p>{{ research.answer || 'Agent 未返回文字结论' }}</p></div>
          <div class="trace">
            <article v-for="(step, index) in research.steps" :key="`${step.kind}-${index}`"><span>{{ index + 1 }}</span><div><strong>{{ step.tool || step.kind }}</strong><p>{{ step.summary }}</p></div></article>
          </div>
          <div class="evidence">
            <h3>可溯源证据（{{ research.evidence.length }}）</h3>
            <article v-for="item in research.evidence" :key="item.chunk_id"><strong>{{ item.title }}</strong><small>{{ item.locator }} · {{ item.revision }}</small><p>{{ item.summary }}</p></article>
          </div>
          <small class="session">WeKnora Session：{{ research.session_id }}</small>
        </div>
        <div v-else class="empty-hint">测试会创建 WeKnora Agent 会话，沿 Wiki/图谱检索，并验证是否返回可引用来源。</div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.header-actions{display:flex;gap:10px}.connection{display:grid;grid-template-columns:.8fr 1.5fr 1.4fr .9fr .7fr;margin-bottom:16px;overflow:hidden}.connection>div{padding:15px 17px;border-right:1px solid var(--semora-line);min-width:0}.connection>div:last-child{border:0}.connection small,.connection strong,.connection code{display:block}.connection small{color:var(--semora-muted);margin-bottom:7px}.connection code{font-size:12px;overflow-wrap:anywhere}.layout{display:grid;grid-template-columns:minmax(340px,.8fr) minmax(520px,1.4fr);gap:16px}.config-panel,.test-panel{overflow:hidden;height:max-content}.form{display:grid;gap:17px;padding:20px}.form label{display:grid;gap:8px;color:var(--semora-muted)}.kb-card{display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:start;border:1px solid var(--semora-line);border-radius:10px;padding:14px}.kb-card>svg{color:var(--semora-green)}.kb-card small{display:block;color:var(--semora-muted);margin-top:4px;font-size:11px;overflow-wrap:anywhere}.kb-card p{margin:9px 0 0;color:var(--semora-muted);line-height:1.55;font-size:12px}.hint,.session{color:var(--semora-muted);font-size:12px;margin:0;line-height:1.55}.searchbar{display:flex;gap:10px;align-items:center;padding:16px;border-bottom:1px solid var(--semora-line)}.searchbar input{flex:1;border:0;background:var(--semora-page);border-radius:8px;padding:11px;outline:0;color:inherit}.research{padding:17px;display:grid;gap:15px}.answer{border-left:3px solid var(--semora-green);padding:2px 12px}.answer small{color:var(--semora-muted)}.answer p{line-height:1.7;margin:6px 0 0}.trace{display:grid;gap:8px}.trace article{display:flex;gap:10px;border:1px solid var(--semora-line);border-radius:8px;padding:10px}.trace article>span{width:22px;height:22px;display:grid;place-items:center;flex:none;border-radius:50%;background:var(--semora-green-soft);color:var(--semora-green-dark);font-size:11px}.trace p,.evidence p{margin:4px 0 0;color:var(--semora-muted);font-size:12px;line-height:1.55}.evidence h3{font-size:14px}.evidence article{border-top:1px solid var(--semora-line);padding:12px 0}.evidence small{display:block;color:var(--semora-muted);margin-top:4px}.empty-hint{padding:42px 24px;text-align:center;color:var(--semora-muted)}@media(max-width:1100px){.connection{grid-template-columns:1fr 1fr}.layout{grid-template-columns:1fr}}
</style>
