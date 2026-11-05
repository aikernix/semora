<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'

const runId = ref('')
const runSearch = ref('')
const statusFilter = ref('')
const runs = ref<any[]>([])
const events = ref<any[]>([])
const verify = ref<any>(null)
const archive = ref<any>(null)
const error = ref('')
const loading = ref(false)
const indexLoading = ref(false)

async function loadRuns() {
  indexLoading.value = true
  try {
    const response = await api.get('/audits', {
      params: {
        q: runSearch.value || undefined,
        status: statusFilter.value || undefined,
        limit: 100,
      },
    })
    runs.value = response.data
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || cause.message
  } finally {
    indexLoading.value = false
  }
}

async function replay(selectedRunId?: string) {
  if (selectedRunId) runId.value = selectedRunId
  error.value = ''
  loading.value = true
  try {
    const [eventResponse, verifyResponse, archiveResponse] = await Promise.all([
      api.get(`/audits/${runId.value}`),
      api.get(`/audits/${runId.value}/verify`),
      api.get(`/audits/${runId.value}/archive/verify`),
    ])
    events.value = eventResponse.data
    verify.value = verifyResponse.data
    archive.value = archiveResponse.data
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || cause.message
  } finally {
    loading.value = false
  }
}

onMounted(loadRuns)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>审计回放</h1>
        <p>验证数据库事件链、外部不可变归档，以及可选 Kafka 合规投递位置。</p>
      </div>
    </div>

    <div class="audit-layout">
      <aside class="run-index">
        <div class="index-title"><strong>查询审计记录</strong><span>{{ runs.length }} 条</span></div>
        <div class="index-filters">
          <t-input v-model="runSearch" placeholder="搜索问题、Run ID 或 Trace ID" @enter="loadRuns" />
          <t-select v-model="statusFilter" clearable placeholder="全部状态" @change="loadRuns">
            <t-option value="completed" label="已完成" />
            <t-option value="failed" label="失败" />
            <t-option value="awaiting_approval" label="待审批" />
            <t-option value="clarifying" label="待澄清" />
          </t-select>
          <t-button variant="outline" :loading="indexLoading" @click="loadRuns">检索</t-button>
        </div>
        <div class="run-list">
          <button
            v-for="item in runs"
            :key="item.id"
            :class="{ active: runId === item.id }"
            :aria-label="`回放 ${item.id}`"
            @click="replay(item.id)"
          >
            <span class="run-heading"><strong>{{ item.question }}</strong><em>{{ item.status }}</em></span>
            <code>{{ item.id }}</code>
            <small>{{ item.owner_username || item.owner_subject || '系统任务' }} · {{ item.event_count }} 个事件</small>
            <small>{{ new Date(item.updated_at).toLocaleString() }} · {{ item.archived ? '已归档' : '待归档' }}</small>
          </button>
          <div v-if="!runs.length && !indexLoading" class="empty-hint">没有符合条件的审计记录。</div>
        </div>
      </aside>

      <section class="replay-panel">
        <div class="audit-search">
          <t-input v-model="runId" placeholder="输入查询 Run ID" @enter="replay()" />
          <t-button theme="primary" :loading="loading" :disabled="!runId.trim()" @click="replay()">
            验证并回放
          </t-button>
        </div>

        <div v-if="verify" class="verification-grid">
      <t-alert
        :theme="verify.valid ? 'success' : 'error'"
        :title="verify.valid ? '数据库哈希链完整' : '数据库哈希链校验失败'"
        :message="`${verify.algorithm} · ${events.length} 个事件`"
      />
      <t-alert
        :theme="archive?.valid ? 'success' : archive?.archived ? 'error' : 'warning'"
        :title="archive?.valid ? '外部归档校验通过' : archive?.archived ? '外部归档校验失败' : '尚未完成外部归档'"
        :message="archive?.valid ? `${archive.event_count} 个事件 · 内容与终态哈希一致` : '归档服务将自动重试终态查询'"
      />
        </div>

        <t-card v-if="archive?.archived" title="不可变归档凭证" :bordered="false" class="archive-card">
      <dl>
        <div><dt>对象位置</dt><dd>{{ archive.object_uri }}</dd></div>
        <div><dt>内容 SHA-256</dt><dd><code>{{ archive.content_hash }}</code></dd></div>
        <div><dt>终态事件哈希</dt><dd><code>{{ archive.terminal_event_hash }}</code></dd></div>
        <div><dt>归档时间</dt><dd>{{ new Date(archive.exported_at).toLocaleString() }}</dd></div>
        <div>
          <dt>WORM 保留至</dt>
          <dd>{{ archive.retention_until ? new Date(archive.retention_until).toLocaleString() : '演示环境未启用；生产默认 Compliance' }}</dd>
        </div>
        <div>
          <dt>对象加密</dt>
          <dd v-if="archive.encryption?.mode !== 'disabled'">
            {{ archive.encryption?.mode }} · {{ archive.encryption?.kms_key_configured ? 'KMS 密钥已配置' : '存储服务托管密钥' }} ·
            {{ archive.encryption?.versioning_required ? '强制版本控制' : '未强制版本控制' }}
          </dd>
          <dd v-else>演示环境未启用；生产默认 aws:kms 并强制版本控制</dd>
        </div>
        <div>
          <dt>Kafka</dt>
          <dd v-if="archive.kafka?.topic">
            {{ archive.kafka.topic }} / {{ archive.kafka.partition }} / {{ archive.kafka.offset }}
          </dd>
          <dd v-else>未配置；对象归档不受影响</dd>
        </div>
      </dl>
        </t-card>

        <t-alert v-if="error" theme="error" :message="error" />

        <div class="audit-line">
      <article v-for="event in events" :key="event.id">
        <span>{{ event.sequence }}</span>
        <div>
          <strong>{{ event.event_type }}</strong>
          <small>{{ event.actor }} · {{ new Date(event.created_at).toLocaleString() }}</small>
          <code>{{ event.event_hash.slice(0, 24) }}…</code>
        </div>
      </article>
        </div>
        <div v-if="!events.length" class="empty-hint">
          从左侧选择一次查询，或直接输入 Run ID 进行验证和回放。
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.audit-layout { display: grid; grid-template-columns: minmax(320px, 390px) minmax(0, 1fr); gap: 18px; align-items: start; }
.run-index, .replay-panel { background: var(--semora-surface); border-radius: 12px; padding: 16px; }
.index-title { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.index-title span { color: var(--semora-muted); font-size: 12px; }
.index-filters { display: grid; grid-template-columns: 1fr 120px auto; gap: 8px; }
.run-list { display: grid; gap: 8px; margin-top: 12px; max-height: calc(100vh - 300px); overflow: auto; }
.run-list button { border: 1px solid var(--semora-line); background: transparent; border-radius: 10px; padding: 11px; text-align: left; color: inherit; cursor: pointer; }
.run-list button:hover, .run-list button.active { border-color: var(--semora-green); background: color-mix(in srgb, var(--semora-green) 7%, transparent); }
.run-heading { display: flex; justify-content: space-between; gap: 8px; }
.run-heading strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-heading em { color: var(--semora-green-deep); font-size: 11px; font-style: normal; }
.run-list code, .run-list small { display: block; color: var(--semora-muted); margin-top: 5px; overflow-wrap: anywhere; }
.audit-search { display: flex; gap: 10px; max-width: 720px; margin-bottom: 16px; }
.verification-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.archive-card { margin-top: 14px; }
.archive-card dl { display: grid; gap: 10px; margin: 0; }
.archive-card dl > div { display: grid; grid-template-columns: 130px minmax(0, 1fr); gap: 12px; }
.archive-card dt { color: var(--semora-muted); }
.archive-card dd { margin: 0; overflow-wrap: anywhere; }
.archive-card code { color: var(--semora-green-deep); }
.audit-line { border-left: 2px solid var(--semora-line); margin: 28px 0 0 16px; }
.audit-line article { display: flex; gap: 15px; margin: 0 0 18px -16px; }
.audit-line article > span { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; background: var(--semora-green); color: white; font-size: 12px; }
.audit-line strong, .audit-line small, .audit-line code { display: block; }
.audit-line small, .audit-line code { color: var(--semora-muted); margin-top: 5px; }
@media (max-width: 860px) {
  .audit-layout { grid-template-columns: 1fr; }
  .run-list { max-height: 420px; }
  .index-filters { grid-template-columns: 1fr; }
  .verification-grid { grid-template-columns: 1fr; }
  .archive-card dl > div { grid-template-columns: 1fr; gap: 3px; }
}
</style>
