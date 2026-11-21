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
const showExportHelp = ref(false)
const modes = computed(() => products.value.find(item=>item.id===product.value)?.modes || [])
const valid = computed(() => name.value.trim() && product.value && ddl.value && (modes.value.length <= 1 || mode.value))

watch(product, () => {mode.value = modes.value.length === 1 ? modes.value[0].id : ''})
watch(() => props.visible, async visible => {
  if (!visible) return
  name.value='';product.value='';mode.value='';version.value='';ddl.value=undefined;documents.value=[];error.value=''
  showExportHelp.value = false
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
      <div class="full file-upload">
        <div class="file-heading">
          <label for="offline-ddl-file">DDL / SQL 文件</label>
          <button class="export-help-button" type="button" aria-label="如何导出表结构"
                  :aria-expanded="showExportHelp" aria-controls="offline-ddl-export-help"
                  @click="showExportHelp = !showExportHelp">?</button>
        </div>
        <input id="offline-ddl-file" :key="'ddl'+fileKey" type="file" name="ddl" accept=".ddl,.sql"
               :disabled="busy" @change="ddl=fileSelection($event)[0]" />
        <div v-show="showExportHelp" id="offline-ddl-export-help" class="export-help">
          <p><strong>如何导出表结构</strong></p>
          <p>选择「仅结构」，保存为 UTF-8 编码的 <code>.sql</code> 或 <code>.ddl</code> 文件。请保留主外键、索引和表字段注释。</p>
          <dl>
            <dt>Navicat</dt><dd>右键数据库或表 → 转储 SQL 文件 → 仅结构</dd>
            <dt>MySQL</dt><dd><code>mysqldump --no-data</code></dd>
            <dt>PostgreSQL</dt><dd><code>pg_dump --schema-only --format=plain</code></dd>
            <dt>SQL Server</dt><dd>SSMS → 任务 → 生成脚本 → 仅架构</dd>
            <dt>Oracle</dt><dd>SQL Developer → Database Export → 仅勾选 Export DDL</dd>
            <dt>OceanBase</dt><dd>ODC → 仅导出结构，或使用 <code>obdumper --ddl</code></dd>
          </dl>
          <p>厂商专有语法的解析情况以实际导出的 SQL 为准。</p>
        </div>
      </div>
      <div class="full file-upload">
        <label><span>资料文件（选填），支持 <small class="file-types">.xlsx、.docx、.txt</small></span>
          <input :key="'docs'+fileKey" type="file" name="documents" accept=".xlsx,.docx,.txt"
                 aria-describedby="offline-document-help" multiple :disabled="busy" @change="documents=fileSelection($event)" />
        </label>
        <p id="offline-document-help" class="document-help">上传表间字段映射、关系基数或字段注释补充。业务口径、术语和规则请移步知识库。</p>
      </div>
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
.file-upload{display:grid;gap:6px}
.file-heading{display:flex;align-items:center;gap:6px}
.file-types,.document-help{color:var(--semora-muted);font-size:12px;font-weight:400}
.document-help{margin:0;line-height:1.6}
.export-help-button{display:grid;place-items:center;width:18px;height:18px;padding:0;border:1px solid currentColor;border-radius:50%;background:transparent;color:var(--semora-muted);font:600 12px/1 system-ui;cursor:pointer}
.export-help-button:hover,.export-help-button[aria-expanded=true]{color:var(--semora-ink)}
.export-help{padding:10px 12px;border:1px solid var(--semora-line);border-radius:6px;color:var(--semora-muted);font-size:12px;line-height:1.6}
.export-help p{margin:0}
.export-help strong,.export-help dt{font-weight:600;color:var(--semora-ink)}
.export-help dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:3px 12px;margin:6px 0}
.export-help dd{margin:0;overflow-wrap:anywhere}
.form-actions{display:flex;justify-content:flex-end;gap:8px;padding-top:16px;border-top:1px solid var(--semora-line)}
@media(max-width:600px){.offline-form{grid-template-columns:1fr}}
</style>
