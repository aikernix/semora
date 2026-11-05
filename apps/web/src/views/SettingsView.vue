<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { api, type SystemSettings } from '../api'

const form = ref<SystemSettings>({
  approval_mode: 'risk_based',
  approve_sensitive_detail: true,
  approve_large_result: true,
  approve_cross_organization: true,
  large_result_threshold: 100_000,
  query_timeout_seconds: 30,
  max_result_rows: 1_000,
  max_result_mb: 10,
  preview_rows: 100,
  reviewer_isolated_context: true,
  reasoning_display: 'structured',
  version: 1,
  policy_version: '',
  updated_at: '',
  updated_by: 'system',
})
const loading = ref(true)
const saving = ref(false)
const modelForm = ref({ base_url: '', model: '', api_key: '' })
const modelStatus = ref({ configured: false, api_key_configured: false })
const modelBusy = ref(false)
const modelMessage = ref('')
const modelError = ref('')

async function loadModel() {
  const { data } = await api.get('/settings/model')
  modelForm.value.base_url = data.base_url
  modelForm.value.model = data.model
  modelStatus.value = data
}
async function configureModel(testOnly: boolean) {
  modelBusy.value = true
  modelMessage.value = ''; modelError.value = ''
  try {
    if (testOnly) {
      await api.post('/settings/model/test', modelForm.value, { timeout: 60_000 })
      modelMessage.value = '连接成功，模型支持结构化 JSON 输出和 Agent 工具调用。请保存配置以启用。'
    } else {
      modelStatus.value = (await api.put('/settings/model', modelForm.value)).data
      modelForm.value.api_key = ''
      modelMessage.value = '模型配置已保存并立即生效。API Key 不会返回浏览器。'
    }
  } catch (error: any) { modelError.value = error?.response?.data?.detail || '模型操作失败' }
  finally { modelBusy.value = false }
}

function apply(value: SystemSettings) { Object.assign(form.value, value) }

async function load() {
  loading.value = true
  try { apply((await api.get<SystemSettings>('/settings')).data) }
  finally { loading.value = false }
}

async function save() {
  saving.value = true
  try {
    apply((await api.put<SystemSettings>('/settings', form.value)).data)
    await MessagePlugin.success('系统策略已发布；新的查询将使用该版本')
  } catch (error: any) {
    await MessagePlugin.error(error?.response?.data?.detail || '保存失败')
  } finally { saving.value = false }
}

onMounted(async () => { await Promise.all([load(), loadModel()]) })
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>系统设置</h1>
        <p>集中管理审批和执行边界。查询用户不能在工作台覆盖这些策略。</p>
      </div>
      <t-button theme="primary" :loading="saving" :disabled="loading" @click="save">发布配置</t-button>
    </div>

    <section id="model" class="panel model-config">
      <div class="panel-title">基础模型配置 <t-tag :theme="modelStatus.configured ? 'success' : 'warning'" variant="light">{{ modelStatus.configured ? '已配置' : '未配置' }}</t-tag></div>
      <div class="form">
        <p class="hint">用于需求理解、逐轮澄清、知识研究和 SQL 复核。请配置支持结构化 JSON 输出的模型。</p>
        <div class="model-fields">
          <label>API 地址（OpenAI 兼容）<t-input v-model="modelForm.base_url" placeholder="https://your-provider.example/v1" /></label>
          <label>模型名称<t-input v-model="modelForm.model" placeholder="填写服务商提供的模型 ID" /></label>
          <label>API Key<t-input v-model="modelForm.api_key" type="password" autocomplete="new-password" :placeholder="modelStatus.api_key_configured ? '已保存；留空保留原 Key' : '输入 API Key'" /></label>
        </div>
        <t-alert v-if="modelError" theme="error" :message="modelError" />
        <t-alert v-if="modelMessage" theme="success" :message="modelMessage" />
        <div class="model-actions"><t-button variant="outline" :loading="modelBusy" @click="configureModel(true)">测试连接</t-button><t-button theme="primary" :loading="modelBusy" @click="configureModel(false)">保存模型配置</t-button></div>
      </div>
    </section>

    <div class="policy-meta panel">
      <div><small>当前策略版本</small><code>{{ form.policy_version || '加载中…' }}</code></div>
      <div><small>配置版本</small><strong>v{{ form.version }}</strong></div>
      <div><small>最后更新</small><strong>{{ form.updated_at ? new Date(form.updated_at).toLocaleString() : '—' }}</strong></div>
      <div><small>发布人</small><strong>{{ form.updated_by }}</strong></div>
    </div>

    <t-loading :loading="loading" show-overlay>
      <div class="settings-grid">
        <section class="panel">
          <div class="panel-title">审批策略</div>
          <div class="form">
            <label>默认模式
              <t-select v-model="form.approval_mode">
                <t-option value="disabled" label="关闭审批"/>
                <t-option value="all" label="全部审批"/>
                <t-option value="risk_based" label="风险审批"/>
              </t-select>
            </label>
            <label>风险触发条件
              <div class="checks">
                <t-checkbox v-model="form.approve_sensitive_detail">敏感字段明细</t-checkbox>
                <t-checkbox v-model="form.approve_large_result">预估超过阈值</t-checkbox>
                <t-checkbox v-model="form.approve_cross_organization">跨机构范围</t-checkbox>
              </div>
            </label>
            <label>大结果集阈值
              <t-input-number v-model="form.large_result_threshold" :min="1000" :max="100000000"/>
            </label>
          </div>
        </section>

        <section class="panel">
          <div class="panel-title">查询硬限制</div>
          <div class="form two">
            <label>超时时间
              <t-input-number v-model="form.query_timeout_seconds" :min="1" :max="30" suffix="秒"/>
            </label>
            <label>最大行数
              <t-input-number v-model="form.max_result_rows" :min="1" :max="1000"/>
            </label>
            <label>最大结果
              <t-input-number v-model="form.max_result_mb" :min="1" :max="10" suffix="MB"/>
            </label>
            <label>页面预览
              <t-input-number v-model="form.preview_rows" :min="1" :max="form.max_result_rows" suffix="行"/>
            </label>
          </div>
          <p class="hint">这些值只能收紧 Executor 的 30 秒、1,000 行和 10 MB 安全上限。</p>
        </section>

        <section class="panel">
          <div class="panel-title">服务</div>
          <div class="service-list">
            <div><span><i class="status-dot"></i>WeKnora Wiki</span><t-tag variant="light">可选辅助</t-tag></div>
            <div><span><i class="status-dot"></i>Semora Ontology Core</span><t-tag theme="success" variant="light">内建</t-tag></div>
            <div><span><i class="status-dot"></i>Semantic Planner</span><t-tag theme="success" variant="light">内建</t-tag></div>
            <div><span><i class="status-dot"></i>Keycloak OIDC</span><t-tag variant="light">身份服务</t-tag></div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-title">模型与 Reviewer</div>
          <div class="form">
            <label>模型协议<t-input value="OpenAI-compatible" readonly/></label>
            <label class="switch-row">Reviewer 隔离上下文<t-switch v-model="form.reviewer_isolated_context"/></label>
            <label>推理展示
              <t-radio-group v-model="form.reasoning_display">
                <t-radio value="structured">结构化依据</t-radio>
                <t-radio value="hidden">仅展示结论</t-radio>
              </t-radio-group>
            </label>
          </div>
        </section>
      </div>
    </t-loading>
  </div>
</template>

<style scoped>
.model-config{margin-bottom:18px;scroll-margin-top:20px}.model-fields{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:16px}.model-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:14px}.model-config .hint{padding:0;margin-bottom:18px}@media(max-width:1100px){.model-fields{grid-template-columns:1fr}}
.policy-meta{display:grid;grid-template-columns:2fr .7fr 1.4fr 1fr;margin-bottom:16px}.policy-meta>div{padding:15px 18px;border-right:1px solid var(--semora-line)}.policy-meta>div:last-child{border-right:0}.policy-meta small,.policy-meta strong,.policy-meta code{display:block}.policy-meta small{color:var(--semora-muted);margin-bottom:7px}.policy-meta code{color:var(--semora-green-dark);font-size:12px}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.form{padding:20px}.form label{display:block;margin-bottom:19px;color:var(--semora-muted)}.form label>.t-select,.form label>.t-input,.form label>.t-input-number{display:block;margin-top:8px;width:100%}.form.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}.checks{display:flex;flex-direction:column;gap:10px;margin-top:10px}.service-list>div{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--semora-line)}.switch-row{display:flex!important;justify-content:space-between;align-items:center}.hint{color:var(--semora-muted);font-size:12px;padding:0 20px 16px;margin:0}@media(max-width:1000px){.settings-grid{grid-template-columns:1fr}.policy-meta{grid-template-columns:1fr 1fr}}
</style>
