<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  BrowseIcon, BookIcon, RelationIcon, DataBaseIcon,
  SettingIcon, ChevronLeftIcon, ChevronRightIcon,
} from 'tdesign-icons-vue-next'
import { authState, hasRole, logout } from './auth'

const route = useRoute()
const router = useRouter()
const collapsed = ref(true)
const dark = ref(false)
const items = computed(() => [
  { path: '/workbench', label: '查询工作台', icon: BrowseIcon, visible: hasRole('business_user', 'analyst', 'admin') },
  { path: '/knowledge', label: '知识库', icon: BookIcon, visible: hasRole('business_user', 'analyst', 'data_steward', 'admin') },
  { path: '/ontology', label: '本体工作室', icon: RelationIcon, visible: hasRole('analyst', 'data_steward', 'admin') },
  { path: '/datasources', label: '数据源', icon: DataBaseIcon, visible: hasRole('analyst', 'data_steward', 'admin') },
  { path: '/settings', label: '模型与设置', icon: SettingIcon, visible: hasRole('admin') },
].filter(item => item.visible))
const roleLabel = computed(() => authState.roles.filter(role => ['analyst', 'approver', 'auditor', 'data_steward', 'admin'].includes(role)).join(' · ') || '业务用户')
const active = computed(() => route.path)
function navigate(path: string) { router.push(path) }
function toggleDark() {
  dark.value = !dark.value
  document.documentElement.setAttribute('theme-mode', dark.value ? 'dark' : 'light')
}
</script>

<template>
  <div class="app-shell">
    <aside class="global-nav" :class="{ collapsed }">
      <div class="brand" @click="navigate('/workbench')">
        <span class="brand-mark"><i></i><i></i><i></i></span>
        <span v-if="!collapsed" class="brand-word">SEMORA</span>
      </div>
      <nav>
        <button v-for="item in items" :key="item.path" :class="{ active: active === item.path }" :title="item.label" :aria-label="item.label" @click="navigate(item.path)">
          <component :is="item.icon" size="22px" />
          <span v-if="!collapsed">{{ item.label }}</span>
        </button>
      </nav>
      <div class="nav-footer">
        <button class="theme-switch" @click="toggleDark">{{ dark ? '浅色' : '深色' }}<span v-if="!collapsed">模式</span></button>
        <button class="user" title="退出登录" @click="logout"><t-avatar size="34px">{{ authState.username.slice(0, 1).toUpperCase() }}</t-avatar><div v-if="!collapsed"><strong>{{ authState.username }}</strong><small>{{ roleLabel }}</small></div></button>
      </div>
      <button class="collapse-button" @click="collapsed = !collapsed">
        <ChevronRightIcon v-if="collapsed" /><ChevronLeftIcon v-else />
      </button>
    </aside>
    <main class="route-view"><router-view /></main>
  </div>
</template>

