<script setup lang="ts">
import { computed } from 'vue'
const props=withDefaults(defineProps<{
  sourceTable:string;targetTable:string;sourceFields:string[];targetFields:string[]
  sourceLabel?:string;targetLabel?:string;cardinality:string;status:string;reason?:string
  condition?:string;optional?:boolean;busy?:boolean;canManage?:boolean
}>(),{busy:false,canManage:true})
const emit=defineEmits<{'edit':[];'decision':[action:'approve'|'reject'|'disable'|'enable']}>()
const known=computed(()=>['1:1','1:N','N:1','N:N'].includes(props.cardinality))
const statusLabel=computed(()=>({accepted:'已生效',pending:'待审核',disabled:'已停用'} as Record<string,string>)[props.status]||props.status)
const conditionText=computed(()=>props.condition||props.sourceFields.map((field,index)=>`${props.sourceTable}.${field} = ${props.targetTable}.${props.targetFields[index]}`).join(' AND '))
</script>

<template>
  <article class="relation-card relationship-card">
    <div class="relation-expression"><span><strong>{{sourceTable}}</strong><code>{{sourceFields.join(', ')}}</code><small v-if="sourceLabel">{{sourceLabel}}</small></span><b>{{cardinality}}</b><span><strong>{{targetTable}}</strong><code>{{targetFields.join(', ')}}</code><small v-if="targetLabel">{{targetLabel}}</small></span></div>
    <p v-if="reason" class="relation-reason">{{reason}}</p>
    <details v-if="conditionText" class="relation-condition"><summary>查看完整关联条件</summary><code>{{conditionText}}</code></details>
    <div class="relation-meta"><t-tag :theme="status==='accepted'?'success':status==='pending'?'warning':'default'" variant="light">{{statusLabel}}</t-tag><t-tag v-if="optional" variant="light">可选关联</t-tag>
      <div v-if="canManage" class="relation-actions">
        <t-button v-if="status==='accepted'||status==='pending'||!known" variant="text" :disabled="busy" @click="emit('edit')">编辑</t-button>
        <t-button v-if="status==='accepted'" variant="text" theme="danger" :disabled="busy" @click="emit('decision','disable')">停用</t-button>
        <template v-else-if="status==='pending'"><small v-if="!known">基数未确定，请先编辑</small><t-button variant="text" theme="primary" :loading="busy" :disabled="busy||!known" @click="emit('decision','approve')">同意</t-button><t-button variant="text" theme="danger" :disabled="busy" @click="emit('decision','reject')">拒绝</t-button></template>
        <t-button v-else variant="text" theme="primary" :loading="busy" :disabled="busy||!known" @click="emit('decision','enable')">启用</t-button>
      </div>
    </div>
  </article>
</template>

<style scoped>
.relation-card.relationship-card{border:1px solid var(--semora-line);border-radius:12px;padding:16px;min-width:0}.relation-expression{display:grid;grid-template-columns:minmax(0,1fr) 60px minmax(0,1fr);gap:14px;align-items:center}.relation-expression>span{min-width:0}.relation-expression strong,.relation-expression code,.relation-expression small{display:block;overflow-wrap:anywhere}.relation-expression code{margin-top:6px;color:#526b88}.relation-expression small{margin-top:6px;color:var(--semora-muted);font-size:12px}.relation-expression>b{text-align:center;color:#173b68}.relation-reason{margin:12px 0 0;color:var(--semora-muted);font-size:13px;line-height:1.6;overflow-wrap:anywhere}.relation-condition{margin-top:12px;font-size:12px;color:var(--semora-muted)}.relation-condition summary{cursor:pointer}.relation-condition code{display:block;margin-top:8px;overflow-wrap:anywhere;white-space:pre-wrap}.relation-meta{display:flex;gap:8px;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--semora-line);color:var(--semora-muted);flex-wrap:wrap}.relation-actions{margin-left:auto;display:flex;align-items:center;flex-wrap:wrap}.relation-actions small{font-size:12px}@media(max-width:600px){.relation-expression{grid-template-columns:minmax(0,1fr) 38px minmax(0,1fr);gap:8px}.relation-actions{margin-left:0}}
</style>
