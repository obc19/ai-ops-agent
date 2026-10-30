<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, fmtTime } from '../api'

const data = ref<any>(null)
const host = ref<any>(null)
const cluster = ref<any>(null)
const topology = ref<any>(null)
const recent = ref<any[]>([])
const loading = ref(true)

async function load() {
  loading.value = true
  try {
    data.value = await api.overview()
    // 服务器状态 / 集群状态 / 拓扑接口失败不应拖垮整页
    host.value = await api.host().catch(() => null)
    cluster.value = await api.cluster().catch(() => null)
    topology.value = await api.topology().catch(() => null)
    const alerts = await api.alerts({ limit: 8 })
    recent.value = alerts.items || []
  } finally {
    loading.value = false
  }
}

function fmtValue(item: any): string {
  const v = item.value
  if (v === null || v === undefined) return '-'
  if (item.unit === '个') return String(Math.round(v))
  return Number(v).toFixed(Number(v) >= 100 ? 0 : 1)
}

function ms(v: number | null): string {
  return v === null || v === undefined ? '-' : `${v}ms`
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
      <h2>
        服务依赖拓扑
        <span v-if="topology?.stats" class="muted" style="font-size: 12px; font-weight: 400; margin-left: 8px">
          {{ topology.stats.nodes }} 个节点 / {{ topology.stats.edges }} 条依赖 · 来源 {{ topology.source }}
        </span>
      </h2>
      <div v-if="!topology" class="empty">未获取到拓扑数据</div>
      <div v-else-if="!topology.edges.length" class="empty">{{ topology.hint || '拓扑为空' }}</div>
      <template v-else>
        <pre class="topo">{{ topology.path }}</pre>
        <div class="row" style="margin-top: 10px">
          <span class="tag ok">异常会沿调用链收敛为单一根因</span>
          <span v-if="topology.stats.has_cycle" class="tag p1">存在循环依赖</span>
          <span class="tag gray">刷新周期 {{ topology.refresh_seconds }}s</span>
        </div>
      </template>
    </div>

    <div class="card">
      <h2>
        服务器状态
        <span v-if="host?.host" class="muted" style="font-size: 12px; font-weight: 400; margin-left: 8px">
          {{ host.host }}
        </span>
      </h2>
      <div v-if="!host" class="empty">未获取到主机数据</div>
      <div v-else-if="!host.available" class="empty">{{ host.reason }}</div>
      <div v-else class="grid">
        <div v-for="item in host.items" :key="item.metric" class="stat compact">
          <div class="label">{{ item.label }}</div>
          <div class="value" :class="{ alarm: item.warn }">
            {{ fmtValue(item) }}<span v-if="item.unit && item.unit !== '个'" class="unit">{{ item.unit }}</span>
          </div>
        </div>
      </div>
    </div>

    <div v-if="host?.probes?.length" class="card">
      <h2>拨测 · 用户视角</h2>
      <table>
        <thead>
          <tr><th>目标</th><th>状态</th><th>总耗时</th><th>DNS</th><th>建连</th><th>TTFB</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in host.probes" :key="p.name">
            <td>
              {{ p.name }}
              <div class="muted mono" style="font-size: 11px">{{ p.url }}</div>
            </td>
            <td>
              <span :class="p.ok ? 'tag ok' : 'tag p0'">{{ p.ok ? '正常' : '失败' }}</span>
            </td>
            <td class="mono">{{ ms(p.total_ms) }}</td>
            <td class="mono">{{ ms(p.dns_ms) }}</td>
            <td class="mono">{{ ms(p.connect_ms) }}</td>
            <td class="mono">{{ ms(p.ttfb_ms) }}</td>
          </tr>
        </tbody>
      </table>
      <div
        v-for="p in host.probes.filter((x: any) => x.error)"
        :key="p.name + '-err'"
        class="muted"
        style="font-size: 12px; margin-top: 8px"
      >
        {{ p.name }}：{{ p.error }}
      </div>
    </div>

    <div v-if="cluster && cluster.available" class="card">
      <h2>
        K8s 集群状态
        <span class="muted" style="font-size: 12px; font-weight: 400; margin-left: 8px">
          {{ cluster.in_cluster ? '集群内 · ServiceAccount 直连' : '集群外 · API 地址' }}
          · metrics-server {{ cluster.metrics_server ? '可用（含 CPU/内存用量）' : '未安装（事件与状态不受影响）' }}
        </span>
      </h2>
      <div class="grid" v-if="cluster.nodes.length">
        <div v-for="n in cluster.nodes" :key="n.name" class="stat compact">
          <div class="label">{{ n.name }}</div>
          <div class="value" :class="{ alarm: !n.ready }">
            {{ n.ready ? 'Ready' : 'NotReady' }}
          </div>
          <div v-if="n.issues.length" class="muted" style="font-size: 11px">
            {{ n.issues.join(' / ') }}
          </div>
        </div>
      </div>
      <table v-if="cluster.workloads.length" style="margin-top: 12px">
        <thead>
          <tr><th>工作负载</th><th>就绪 / 期望</th><th>缺口</th></tr>
        </thead>
        <tbody>
          <tr v-for="w in cluster.workloads" :key="w.namespace + w.name">
            <td class="mono">{{ w.namespace }}/{{ w.name }}</td>
            <td class="mono">{{ w.ready }} / {{ w.desired }}</td>
            <td><span class="tag p1">缺 {{ w.gap }}</span></td>
          </tr>
        </tbody>
      </table>
      <table v-if="cluster.pod_issues.length" style="margin-top: 12px">
        <thead>
          <tr><th>异常 Pod</th><th>重启</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in cluster.pod_issues" :key="p.namespace + p.name">
            <td class="mono">{{ p.namespace }}/{{ p.name }}</td>
            <td class="mono">{{ p.restarts || '-' }}</td>
            <td>
              <span v-for="r in p.reasons" :key="r" class="tag p0">{{ r }}</span>
              <span v-if="p.not_ready" class="tag p1">未就绪</span>
              <span v-if="!p.reasons.length && !p.not_ready" class="muted">-</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div
        v-if="!cluster.nodes.length && !cluster.workloads.length && !cluster.pod_issues.length"
        class="empty"
      >
        集群当前无异常对象
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
