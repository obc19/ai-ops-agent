<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, fmtTime } from '../api'

const items = ref<any[]>([])
const total = ref(0)
const severity = ref('')
const status = ref('')
const detail = ref('')
const selected = ref<any>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const params: Record<string, any> = { limit: 100 }
    if (severity.value) params.severity = severity.value
    if (status.value) params.status = status.value
    const data = await api.alerts(params)
    items.value = data.items || []
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function open(alert: any) {
  selected.value = alert
  detail.value = await api.alertMarkdown(alert.id)
}

onMounted(load)
</script>

<template>
  <div class="card">
    <div class="row" style="margin-bottom: 12px">
      <select v-model="severity" @change="load">
        <option value="">全部级别</option>
        <option value="P0">P0</option>
        <option value="P1">P1</option>
        <option value="P2">P2</option>
      </select>
      <select v-model="status" @change="load">
        <option value="">全部状态</option>
        <option value="triggered">待处理</option>
        <option value="confirmed">处置中</option>
        <option value="resolved">已关闭</option>
      </select>
      <button @click="load">刷新</button>
      <div class="spacer"></div>
      <span class="muted">共 {{ total }} 条</span>
    </div>

    <table v-if="items.length">
      <thead>
        <tr><th>级别</th><th>对象</th><th>指标</th><th>摘要</th><th>状态</th><th>次数</th><th>最近</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in items" :key="a.id" @click="open(a)">
          <td><span :class="'tag ' + a.severity.toLowerCase()">{{ a.severity }}</span></td>
          <td>{{ a.cluster }}/{{ a.namespace }}/{{ a.target }}</td>
          <td class="mono">{{ a.metric }}</td>
          <td>{{ a.summary }}</td>
          <td><span class="tag gray">{{ a.status }}</span></td>
          <td>×{{ a.count }}</td>
          <td class="muted">{{ fmtTime(a.last_seen_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">{{ loading ? '加载中…' : '暂无告警' }}</div>
  </div>

  <div v-if="selected" class="card">
    <div class="row" style="margin-bottom: 10px">
      <h2 style="margin: 0">告警 {{ selected.id }}</h2>
      <div class="spacer"></div>
      <button @click="selected = null; detail = ''">关闭</button>
    </div>
    <div class="row" style="margin-bottom: 10px">
      <span :class="'tag ' + selected.severity.toLowerCase()">{{ selected.severity }}</span>
      <span class="tag gray">诊断 {{ selected.diagnosis_id || '-' }}</span>
      <span v-if="selected.degraded" class="tag p1">诊断降级</span>
    </div>
    <pre class="markdown">{{ detail }}</pre>
  </div>
</template>
