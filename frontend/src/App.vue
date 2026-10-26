<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, getToken, setToken } from './api'

const health = ref<any>(null)
const token = ref(getToken())
const tokenInput = ref(getToken())
const error = ref('')

async function loadHealth() {
  try {
    health.value = await api.health()
    error.value = ''
  } catch (e: any) {
    error.value = e.message || String(e)
  }
}

function saveToken() {
  setToken(tokenInput.value.trim())
  token.value = tokenInput.value.trim()
  loadHealth()
  location.reload()
}

onMounted(loadHealth)
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">ai-ops-agent</div>
      <div class="version">v0.1.0 · 只读控制台</div>
      <router-link to="/">总览</router-link>
      <router-link to="/alerts">告警</router-link>
      <router-link to="/diagnoses">诊断</router-link>
      <router-link to="/knowledge">知识库</router-link>
      <router-link to="/audit">审计</router-link>
      <div class="mode">
        <div>运行模式：{{ health?.mock_mode ? 'Mock' : '真实数据源' }}</div>
        <div>调度：{{ health?.scheduler_running ? '运行中' : '未启动' }}</div>
        <div>已跑 {{ health?.scheduler_rounds ?? 0 }} 轮</div>
        <div>运行 {{ health?.uptime_seconds ?? 0 }}s</div>
      </div>
    </aside>

    <main class="main">
      <div class="header">
        <h1>{{ $route.meta.title }}</h1>
        <div class="actions">
          <input v-model="tokenInput" placeholder="访问令牌（viewer / operator / admin，未启用可留空）" style="width: 220px" />
          <button @click="saveToken">保存</button>
        </div>
      </div>
      <div v-if="error" class="error">{{ error }}</div>
      <router-view />
    </main>
  </div>
</template>
