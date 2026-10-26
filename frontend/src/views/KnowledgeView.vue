<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, fmtTime } from '../api'

const items = ref<any[]>([])
const stats = ref<any>(null)
const status = ref('')
const query = ref('')
const hits = ref<any[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const params: Record<string, any> = { limit: 200 }
    if (status.value) params.status = status.value
    items.value = await api.knowledge(params)
    stats.value = await api.knowledgeStats()
  } finally {
    loading.value = false
  }
}

async function search() {
  if (!query.value.trim()) return
  const resp = await fetch('/api/v1/knowledge/search?q=' + encodeURIComponent(query.value), {
    headers: localStorage.getItem('aiops_token')
      ? { Authorization: 'Bearer ' + localStorage.getItem('aiops_token') }
      : {}
  })
  const data = await resp.json()
  hits.value = data.hits || []
}

onMounted(load)
</script>

<template>
  <div class="grid" v-if="stats">
    <div class="stat"><div class="label">累计经验</div><div class="value">{{ stats.total }}</div></div>
    <div class="stat"><div class="label">待确认草稿</div><div class="value">{{ stats.draft }}</div></div>
    <div class="stat"><div class="label">已确认</div><div class="value">{{ stats.confirmed }}</div></div>
    <div class="stat"><div class="label">已回滚</div><div class="value">{{ stats.rolled_back }}</div></div>
  </div>

  <div class="card">
    <h2>语义检索</h2>
    <div class="row">
      <input v-model="query" placeholder="描述现象，例如：CPU 打满 线程池耗尽" style="flex: 1; min-width: 240px" @keyup.enter="search" />
      <button @click="search">检索</button>
    </div>
    <table v-if="hits.length" style="margin-top: 12px">
      <thead><tr><th>相似度</th><th>标题</th><th>根因</th></tr></thead>
      <tbody>
        <tr v-for="h in hits" :key="h.experience_id">
          <td>{{ h.score.toFixed(3) }}</td>
          <td>{{ h.title }}</td>
          <td class="muted">{{ h.root_cause }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else-if="query" class="empty">未命中（仅已确认经验参与检索）</div>
  </div>

  <div class="card">
    <div class="row" style="margin-bottom: 12px">
      <select v-model="status" @change="load">
        <option value="">全部状态</option>
        <option value="draft">待确认</option>
        <option value="confirmed">已确认</option>
        <option value="rolled_back">已回滚</option>
      </select>
      <button @click="load">刷新</button>
      <div class="spacer"></div>
      <span class="muted">确认/回滚请在 CLI 或 API 操作（控制台只读）</span>
    </div>
    <table v-if="items.length">
      <thead><tr><th>标题</th><th>状态</th><th>置信度</th><th>确认人</th><th>向量来源</th><th>创建时间</th></tr></thead>
      <tbody>
        <tr v-for="e in items" :key="e.id">
          <td>{{ e.title }}</td>
          <td>
            <span :class="e.status === 'confirmed' ? 'tag ok' : e.status === 'draft' ? 'tag p1' : 'tag gray'">
              {{ e.status }}
            </span>
          </td>
          <td>{{ (e.confidence || 0).toFixed(2) }}</td>
          <td>{{ e.confirmed_by || '-' }}</td>
          <td class="muted mono">{{ e.embedding_source || '-' }}</td>
          <td class="muted">{{ fmtTime(e.created_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">{{ loading ? '加载中…' : '暂无经验' }}</div>
  </div>
</template>
