<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { QueryRun } from '../../api'
import { publicStatus, terminalStatus } from '../../queryPresentation'

const props = defineProps<{ run: QueryRun; connection: string; lastSignal: number }>()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval>
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => clearInterval(timer))
const active = computed(() => !terminalStatus(props.run.status))
const stale = computed(() => active.value && props.lastSignal > 0 && now.value - props.lastSignal > 20_000)
const reconnecting = computed(() => active.value && (stale.value || props.connection === 'reconnecting'))
const elapsed = computed(() => {
  const start = Date.parse(props.run.created_at || props.run.events[0]?.created_at || '')
  const end = active.value ? now.value : Date.parse(props.run.events.at(-1)?.created_at || '')
  if (!Number.isFinite(start) || !Number.isFinite(end)) return ''
  const seconds = Math.max(0, Math.floor((end - start) / 1000))
  const units = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
  return (units[0] ? units : units.slice(1)).map(v => String(v).padStart(2, '0')).join(':')
})
const signalLabel = computed(() => {
  if (!active.value) return props.run.status === 'clarifying' ? '完成确认后继续处理' : ''
  if (reconnecting.value) return '连接恢复中 · 正在重新获取本次进度'
  if (props.connection !== 'live') return '正在连接服务'
  const seconds = Math.max(0, Math.floor((now.value - props.lastSignal) / 1000))
  return `实时连接正常 · ${seconds < 2 ? '刚刚' : seconds + '秒前'}收到响应`
})
</script>

<template>
  <div class="task-status" :class="{ active, reconnecting, failed: run.status === 'failed', waiting: run.status === 'clarifying' }">
    <div class="status-heading" role="status" aria-live="polite"><span class="status-symbol"><span v-if="active && !reconnecting" class="status-spinner"></span><span v-else-if="run.status === 'completed'">✓</span><span v-else-if="run.status === 'failed'">!</span><span v-else>●</span></span><strong>{{ publicStatus(run) }}</strong></div>
    <div class="status-meta"><span v-if="elapsed">任务历时 {{ elapsed }}</span><span v-if="signalLabel">{{ signalLabel }}</span></div>
  </div>
</template>

<style scoped>
.task-status{position:sticky;top:0;z-index:4;background:var(--semora-card);display:flex;align-items:center;justify-content:space-between;gap:16px;padding:15px 0;color:var(--semora-green-dark)}.status-heading{display:flex;align-items:center;gap:9px}.status-heading strong{font-size:14px;font-weight:600}.status-symbol{display:grid;place-items:center;width:19px;height:19px;font-size:14px}.status-spinner{width:15px;height:15px;border:2px solid var(--semora-line);border-top-color:var(--semora-green);border-radius:50%;animation:status-spin 1s linear infinite}.status-meta{display:flex;gap:14px;flex-wrap:wrap;color:var(--semora-muted);font-size:12px;font-variant-numeric:tabular-nums}.failed{color:#c24951}.waiting,.reconnecting{color:#a26a1b}@keyframes status-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.status-spinner{animation:none}}@media(max-width:800px){.task-status{align-items:flex-start;flex-direction:column;gap:6px}.status-meta{padding-left:28px}}
</style>
