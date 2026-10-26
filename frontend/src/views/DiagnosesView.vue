<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, fmtTime } from '../api'

const items = ref<any[]>([])
const detail = ref('')
const selected = ref<any>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    items.value = await api.diagnoses({ limit: 50 })
  } finally {
    loading.value = false
  }
}

async function open(report: any) {
  selected.value = report
  detail.value = await api.diagnosisMarkdown(report.id)
}

onMounted(load)
</script>

<template>
  <div class="card">
    <div class="row" style="margin-bottom: 12px">
      <button @click="load">刷新</button>
      <div class="spacer"></div>
      <span class="muted">最近 {{ items.length }} 次诊断</span>
    </div>
    <table v-if="items.length">
      <thead>
        <tr><th>报告</th><th>对象</th><th>状态</th><th>置信度</th><th>耗时</th><th>根因</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in items" :key="r.id" @click="open(r)">
          <td class="mono">{{ r.id.slice(0, 16) }}</td>
          <td>{{ r.cluster }}/{{ r.target }}</td>
          <td>
            <span :class="r.status === 'ok' ? 'tag ok' : r.status === 'partial' ? 'tag p1' : 'tag p0'">
              {{ r.status }}
            </span>
          </td>
          <td>{{ r.confidence.toFixed(2) }}</td>
          <td>{{ (r.duration_ms / 1000).toFixed(2) }}s</td>
          <td class="muted">{{ (r.root_cause || '').slice(0, 42) }}</td>
          <td class="muted">{{ fmtTime(r.created_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">{{ loading ? '加载中…' : '暂无诊断记录' }}</div>
  </div>

  <div v-if="selected" class="card">
    <div class="row" style="margin-bottom: 10px">
      <h2 style="margin: 0">诊断报告 {{ selected.id }}</h2>
      <div class="spacer"></div>
      <span v-if="selected.used_llm" class="tag ok">LLM 参与</span>
      <span v-else class="tag gray">规则引擎</span>
      <span v-if="selected.rag_hits.length" class="tag p2">命中 {{ selected.rag_hits.length }} 条经验</span>
      <button @click="selected = null; detail = ''">关闭</button>
    </div>
    <pre class="markdown">{{ detail }}</pre>
  </div>
</template>
