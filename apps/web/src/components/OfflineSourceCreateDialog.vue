<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { offlineApi, offlineErrorMessage, type OfflineProduct } from '../offlineSources'

const props = defineProps<{visible:boolean}>()
const emit = defineEmits<{'update:visible':[value:boolean];created:[id:string]}>()
const products = ref<OfflineProduct[]>([])
const product = ref('')
const mode = ref('')
const name = ref('')
const version = ref('')
const ddl = ref<File>()
const documents = ref<File[]>([])
const busy = ref(false)
const loadingProducts = ref(false)
const error = ref('')
const fileKey = ref(0)
const modes = computed(() => products.value.find(item=>item.id===product.value)?.modes || [])
const valid = computed(() => name.value.trim() && product.value && ddl.value && (modes.value.length <= 1 || mode.value))

watch(product, () => {mode.value = modes.value.length === 1 ? modes.value[0].id : ''})
watch(() => props.visible, async visible => {
  if (!visible) return
  name.value='';product.value='';mode.value='';version.value='';ddl.value=undefined;documents.value=[];error.value=''
  fileKey.value++
  if (products.value.length) return
  loadingProducts.value = true
  try { products.value = (await offlineApi.get<OfflineProduct[]>('/products')).data }
  catch (reason) {error.value = offlineErrorMessage(reason,'无法读取数据库类型')}
  finally {loadingProducts.value = false}
})
function fileSelection(event:Event) {
  return Array.from((event.target as HTMLInputElement).files || [])
}
async function submit() {
  if (busy.value || !valid.value || !ddl.value) return
  busy.value=true;error.value=''
  const data = new FormData()
  data.append('name',name.value.trim())
  data.append('product',product.value)
  if (mode.value) data.append('compatibility_mode',mode.value)
  if (version.value.trim()) data.append('version',version.value.trim())
  data.append('ddl',ddl.value)
  data.append('auto_confirm','true')
  documents.value.forEach(file => data.append('documents',file))
  try {
    const result = await offlineApi.post<{id:string}>('/imports',data)
    emit('update:visible',false)
    emit('created',result.data.id)
  } catch (reason) {error.value = offlineErrorMessage(reason,'导入离线数据源失败')}
  finally {busy.value=false}
}
</script>

<template>
  <t-dialog :visible="visible" header="添加离线数据源" width="min(700px,94vw)" :footer="false"
            :close-btn="!busy" :close-on-overlay-click="!busy" :close-on-esc-keydown="!busy" @close="emit('update:visible',false)">
    <form class="offline-form management-controls" @submit.prevent="submit">
      <t-alert v-if="error" theme="error" :message="error" />
      <label class="full">数据源名称<t-input v-model="name" :maxlength="120" :disabled="busy" /></label>
      <label>数据库类型<t-select v-model="product" :loading="loadingProducts" :options="products.map(p=>({value:p.id,label:p.label}))" :disabled="busy" placeholder="请选择数据库类型" /></label>
      <label>数据库版本（选填）<t-input v-model="version" :maxlength="80" :disabled="busy" /></label>
      <label v-if="modes.length>1">兼容模式<t-select v-model="mode" :options="modes.map(m=>({value:m.id,label:m.label}))" :disabled="busy" placeholder="请选择兼容模式" /></label>
      <label class="full">DDL 文件<input :key="'ddl'+fileKey" type="file" name="ddl" accept=".ddl,.sql" :disabled="busy" @change="ddl=fileSelection($event)[0]" /></label>
      <label class="full">资料文件（选填）<input :key="'docs'+fileKey" type="file" name="documents" accept=".xlsx,.docx,.txt,.sql" multiple :disabled="busy" @change="documents=fileSelection($event)" /></label>
      <div class="full form-actions">
        <t-button variant="outline" :disabled="busy" @click="emit('update:visible',false)">取消</t-button>
        <t-button theme="primary" type="submit" :loading="busy" :disabled="!valid">确定</t-button>
      </div>
    </form>
  </t-dialog>
</template>

<style scoped>
.offline-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.offline-form label{display:flex;flex-direction:column;gap:6px;color:var(--semora-ink);font-size:13px;font-weight:600}
.offline-form .full,.offline-form :deep(.t-alert){grid-column:1/-1}
.offline-form input[type=file]{width:100%;box-sizing:border-box;padding:9px;border:1px solid var(--semora-line);border-radius:6px}
.form-actions{display:flex;justify-content:flex-end;gap:8px;padding-top:16px;border-top:1px solid var(--semora-line)}
@media(max-width:600px){.offline-form{grid-template-columns:1fr}}
</style>
