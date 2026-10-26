<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'

const items = ref<any[]>([])
const egressOnly = ref(false)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const data = await api.audit({ limit: 200, egress_only: egressOnly.value })
    items.value = data.items || []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="card">
    <div class="row" style="margin-bottom: 12px">
      <label class="row" style="gap: 6px">
        <input type="checkbox" v-model="egressOnly" @change="load" />
        <span>只看数据外发</span>
      </label>
      <button @click="load">刷新</button>
      <div class="spacer"></div>
      <span class="muted">共 {{ items.length }} 条</span>
    </div>
    <table v-if="items.length">
      <thead><tr><th>时间</th><th>类型</th><th>动作</th><th>调用方</th><th>对象</th><th>详情</th></tr></thead>
      <tbody>
        <tr v-for="item in items" :key="item.seq">
          <td class="muted mono">{{ item.ts }}</td>
          <td>
            <span :class="item.egress ? 'tag p1' : 'tag gray'">{{ item.egress ? '外发' : '内部' }}</span>
          </td>
          <td class="mono">{{ item.action }}</td>
          <td>{{ item.actor }}</td>
          <td class="muted">{{ item.target || '-' }}</td>
          <td class="muted mono">{{ JSON.stringify(item.detail).slice(0, 110) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">{{ loading ? '加载中…' : '暂无审计记录' }}</div>
  </div>
</template>
