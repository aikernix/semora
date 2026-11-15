<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'
const props=defineProps<{visible:boolean;products:any[];drivers:any[]}>()
const emit=defineEmits<{'update:visible':[value:boolean];created:[value:any]}>()
const defaults=()=>({name:'',type:'postgresql',host:'',port:5432,database:'',username:'',password:'',schema_name:'',catalog:'',compatibility_mode:'',service_kind:'service',jdbc_url:'',driver_id:'',product_override:''})
const form=ref(defaults()),properties=ref('{}'),receipt=ref(''),error=ref(''),message=ref('')
const state=ref<'idle'|'testing'|'success'|'error'>('idle'),creating=ref(false),testedSignature=ref('')
const busy=computed(()=>creating.value||state.value==='testing')
const productOptions=computed(()=>props.products.filter(product=>product.id!=='sqlserver'))
const signature=computed(()=>JSON.stringify({...form.value,name:undefined,properties:properties.value}))
const valid=computed(()=>state.value==='success'&&!!receipt.value&&testedSignature.value===signature.value)
watch(signature,()=>{receipt.value='';testedSignature.value='';if(state.value!=='testing'){state.value='idle';message.value=''}})
watch(()=>props.visible,visible=>{if(visible){form.value=defaults();properties.value='{}';receipt.value='';testedSignature.value='';error.value='';message.value='';state.value='idle'}})
function selectProduct(){const product=props.products.find(p=>p.id===form.value.type);form.value.port=product?.port||5432;form.value.driver_id=product?.driver_id||'';form.value.compatibility_mode=form.value.type==='oceanbase'?'mysql':''}
function body(){return {name:form.value.name,type:form.value.type,test_receipt:receipt.value||null,connection:{...Object.fromEntries(Object.entries(form.value).filter(([key])=>key!=='name')),port:Number(form.value.port),properties:JSON.parse(properties.value||'{}'),config_version:2}}}
async function testConnection(){
  if(busy.value)return
  state.value='testing';error.value='';message.value='';const requestedSignature=signature.value
  try{
    const result=(await api.post('/datasource-connection-test',body())).data
    if(requestedSignature!==signature.value){state.value='idle';return}
    receipt.value=result.test_receipt;testedSignature.value=requestedSignature;state.value='success'
    message.value=`连接成功${result.database?` · ${result.database}`:''}${result.version?` · ${result.version}`:''}`
  }catch(reason:any){state.value='error';receipt.value='';error.value=reason.response?.data?.detail||reason.message||'连接失败'}
}
async function createSource(){
  if(!valid.value||!form.value.name.trim()||busy.value)return
  creating.value=true;error.value=''
  try{const result=(await api.post('/datasources',body())).data;emit('update:visible',false);emit('created',result)}
  catch(reason:any){error.value=reason.response?.data?.detail||reason.message||'创建失败'}
  finally{creating.value=false}
}
</script>

<template>
  <t-dialog :visible="visible" header="添加数据源" width="min(800px,94vw)" :footer="false" :close-btn="!busy" :close-on-overlay-click="!busy" :close-on-esc-keydown="!busy" @close="emit('update:visible',false)">
    <div class="create-source-form"><p>填写数据库连接信息，测试通过后创建。创建后会自动获取数据库版本与表结构。</p>
      <t-alert v-if="error" theme="error" :message="error" /><t-alert v-if="message" theme="success" :message="message" />
      <div class="form-grid">
        <label class="span-2">数据源名称<t-input v-model="form.name" :disabled="busy" placeholder="填写便于识别的名称" /></label>
        <label>数据库类型<t-select v-model="form.type" :disabled="busy" :options="productOptions.map(p=>({label:p.label,value:p.id}))" @change="selectProduct" /></label>
        <label>数据库地址<t-input v-model="form.host" :disabled="busy" /></label>
        <label>端口<t-input-number v-model="form.port" :min="1" :max="65535" :disabled="busy" /></label>
        <label>{{form.type==='oracle'?'服务名（Service Name）':'数据库名'}}<t-input v-model="form.database" :disabled="busy" :placeholder="form.type==='oracle'?'例如：XEPDB1':''" /></label>
        <label>用户名<t-input v-model="form.username" :disabled="busy" autocomplete="off" /></label>
        <label>密码<t-input v-model="form.password" type="password" :disabled="busy" autocomplete="new-password" /></label>
      </div>
      <details class="advanced"><summary>高级连接配置</summary><div class="form-grid">
        <label>Schema<t-input v-model="form.schema_name" :disabled="busy" /></label><label>Catalog<t-input v-model="form.catalog" :disabled="busy" /></label>
        <label v-if="form.type==='jdbc'">JDBC 驱动<t-select v-model="form.driver_id" :disabled="busy" :options="drivers.filter(d=>d.enabled).map(d=>({value:d.id,label:`${d.name} ${d.version}`}))" /></label>
        <label v-if="form.type==='oceanbase'">兼容模式<t-select v-model="form.compatibility_mode" :disabled="busy" :options="[{value:'mysql',label:'MySQL'},{value:'oracle',label:'Oracle'}]" /></label>
        <label v-if="form.type==='jdbc'" class="span-2">JDBC 连接串<t-input v-model="form.jdbc_url" :disabled="busy" /></label>
        <label class="span-2">连接属性 JSON<t-textarea v-model="properties" :disabled="busy" /></label>
      </div></details>
      <div class="form-actions"><t-button :disabled="busy" @click="emit('update:visible',false)">取消</t-button><t-button variant="outline" :loading="state==='testing'" :disabled="creating" @click="testConnection">测试连接</t-button><t-button theme="primary" :loading="creating" :disabled="!valid||!form.name.trim()||busy" @click="createSource">创建数据源</t-button></div>
    </div>
  </t-dialog>
</template>

<style scoped>
.create-source-form{display:flex;flex-direction:column;gap:20px}.create-source-form>p{color:var(--semora-muted);font-size:13px;margin:0;line-height:1.7}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 20px}.form-grid label{display:flex;flex-direction:column;gap:8px;color:var(--semora-ink);font-weight:600;min-width:0}.span-2{grid-column:1/-1}.advanced summary{cursor:pointer;color:var(--semora-muted)}.advanced .form-grid{margin-top:18px}.form-actions{display:flex;justify-content:flex-end;gap:10px;padding-top:16px;border-top:1px solid var(--semora-line)}@media(max-width:600px){.form-grid{grid-template-columns:1fr}.span-2{grid-column:auto}}
</style>
