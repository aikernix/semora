<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'

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

onMounted(loadModel)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>系统设置</h1>
        <p>配置基础模型，用于需求理解、知识辅助分析和 SQL 生成。</p>
      </div>
    </div>

    <section id="model" class="panel model-config">
      <div class="panel-title">基础模型配置 <t-tag :theme="modelStatus.configured ? 'success' : 'warning'" variant="light">{{ modelStatus.configured ? '已配置' : '未配置' }}</t-tag></div>
      <div class="form">
        <p class="hint">用于需求理解、逐轮澄清、知识研究和 SQL 说明。请配置支持结构化 JSON 输出的模型。</p>
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

  </div>
</template>

<style scoped>
.model-config{margin-bottom:18px;scroll-margin-top:20px}.model-fields{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:16px}.model-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:14px}.model-config .hint{padding:0;margin-bottom:18px}@media(max-width:1100px){.model-fields{grid-template-columns:1fr}}
.policy-meta{display:grid;grid-template-columns:2fr .7fr 1.4fr 1fr;margin-bottom:16px}.policy-meta>div{padding:15px 18px;border-right:1px solid var(--semora-line)}.policy-meta>div:last-child{border-right:0}.policy-meta small,.policy-meta strong,.policy-meta code{display:block}.policy-meta small{color:var(--semora-muted);margin-bottom:7px}.policy-meta code{color:var(--semora-green-dark);font-size:12px}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.form{padding:20px}.form label{display:block;margin-bottom:19px;color:var(--semora-muted)}.form label>.t-select,.form label>.t-input,.form label>.t-input-number{display:block;margin-top:8px;width:100%}.form.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}.checks{display:flex;flex-direction:column;gap:10px;margin-top:10px}.service-list>div{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--semora-line)}.switch-row{display:flex!important;justify-content:space-between;align-items:center}.hint{color:var(--semora-muted);font-size:12px;padding:0 20px 16px;margin:0}@media(max-width:1000px){.settings-grid{grid-template-columns:1fr}.policy-meta{grid-template-columns:1fr 1fr}}
</style>
