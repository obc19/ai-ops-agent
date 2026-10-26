<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, fmtTime } from '../api'

const data = ref<any>(null)
const recent = ref<any[]>([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    data.value = await api.overview()
    const alerts = await api.alerts({ limit: 8 })
    recent.value = alerts.items || []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-if="loading" class="empty">加载中…</div>
  <template v-else-if="data">
    <div class="grid">
      <div class="stat">
        <div class="label">活跃告警</div>
        <div class="value">{{ data.stats.alerts_active }}</div>
      </div>
      <div class="stat">
        <div class="label">告警总数</div>
        <div class="value">{{ data.stats.alerts_total }}</div>
      </div>
      <div class="stat">
        <div class="label">诊断次数</div>
        <div class="value">{{ data.stats.diagnoses_total }}</div>
      </div>
      <div class="stat">
        <div class="label">已确认经验</div>
        <div class="value">{{ data.stats.experiences_confirmed }}</div>
      </div>
      <div class="stat">
        <div class="label">集群数</div>
        <div class="value">{{ data.stats.clusters_total }}</div>
      </div>
    </div>

    <div class="card">
      <h2>告警分级</h2>
      <div class="row">
        <span class="tag p0">P0 · {{ data.severity_count.P0 || 0 }}</span>
        <span class="tag p1">P1 · {{ data.severity_count.P1 || 0 }}</span>
        <span class="tag p2">P2 · {{ data.severity_count.P2 || 0 }}</span>
      </div>
    </div>

    <div class="card">
      <h2>数据源与运行模式</h2>
      <table>
        <thead>
          <tr><th>数据源</th><th>已配置</th><th>可达</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="src in data.sources" :key="src.name">
            <td>{{ src.name }}</td>
            <td>{{ src.configured ? '是' : '否' }}</td>
            <td>
              <span :class="src.reachable ? 'tag ok' : 'tag gray'">
                {{ src.reachable ? '可达' : '不可达' }}
              </span>
            </td>
            <td class="muted">{{ src.detail || '-' }}</td>
          </tr>
        </tbody>
      </table>
      <div class="row" style="margin-top: 12px">
        <span class="tag gray">mock={{ data.mode.mock }}</span>
        <span class="tag gray">LLM={{ data.mode.llm_enabled ? '已配置' : '未配置（走规则）' }}</span>
        <span class="tag gray">embedding={{ data.mode.embedding_enabled ? '外部' : '本地哈希' }}</span>
        <span class="tag gray">向量库={{ data.mode.knowledge_backend }}</span>
        <span class="tag gray">链路 Agent={{ data.mode.trace_agent ? '启用' : '关闭' }}</span>
        <span class="tag gray">检测周期={{ data.mode.detect_interval }}s</span>
      </div>
      <div v-if="data.mock_faults.length" class="row" style="margin-top: 10px">
        <span class="muted">当前 mock 故障：</span>
        <span v-for="f in data.mock_faults" :key="f.target + f.metric" class="tag p1">
          {{ f.target }} / {{ f.metric }}
        </span>
      </div>
    </div>

    <div class="card">
      <h2>最近告警</h2>
      <table v-if="recent.length">
        <thead>
          <tr><th>级别</th><th>对象</th><th>指标</th><th>摘要</th><th>次数</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="a in recent" :key="a.id" @click="$router.push('/alerts')">
            <td><span :class="'tag ' + a.severity.toLowerCase()">{{ a.severity }}</span></td>
            <td>{{ a.cluster }}/{{ a.target }}</td>
            <td class="mono">{{ a.metric }}</td>
            <td>{{ a.summary }}</td>
            <td>×{{ a.count }}</td>
            <td class="muted">{{ fmtTime(a.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无告警</div>
    </div>
  </template>
</template>
