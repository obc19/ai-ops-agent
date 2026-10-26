<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, getToken, setToken } from './api'

const health = ref<any>(null)
const token = ref(getToken())
const tokenInput = ref(getToken())
const error = ref('')

const testing = ref(false)
const testResult = ref<any>(null)

const llm = computed(() => health.value?.llm || null)
const configured = computed(() => !!llm.value?.configured)

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

async function testLlm() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await api.llmTest()
  } catch (e: any) {
    testResult.value = { ok: false, error: e.message || String(e), latency_ms: 0 }
  } finally {
    testing.value = false
  }
}

onMounted(loadHealth)
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">ai-ops-agent</div>
      <div class="version">v{{ health?.version || '—' }} · 只读控制台</div>
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
        <div :class="configured ? 'llm-ok' : 'llm-off'">
          模型：{{ configured ? llm.model : '未接入（规则模式）' }}
        </div>
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

      <div v-if="health && !configured" class="banner banner-warn">
        <div class="banner-head">
          <strong>当前没有接入大模型 —— 这时的「AI 分析」其实走的是内置规则</strong>
          <button class="ghost" @click="loadHealth">重新检测</button>
        </div>
        <p>
          规则模式可以正常跑通全链路（采集 → 检测 → 告警 → 报告），但它不会推理，
          只会命中固定条件时套模板。要让它真的“分析”，把密钥配上再重启：
        </p>
        <pre class="banner-code">LLM_API_KEY=sk-你的密钥
# 可选（默认已是 DeepSeek，换成别的 OpenAI 兼容服务时再改）
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat</pre>
        <p class="banner-tip">
          Docker 用 <code>-e LLM_API_KEY=sk-xxx</code>；K8s 改 <code>deploy/k8s/20-workload.yaml</code> 的 Secret。
          重启后本横幅消失，右上角「测试连接」可当场验证。
        </p>
      </div>

      <div v-else-if="health" class="banner banner-ok">
        <div class="banner-head">
          <span>
            模型已接入：<strong>{{ llm.model }}</strong> @ {{ llm.provider }}
            <span v-if="llm.api_key_masked" class="dim">（密钥 {{ llm.api_key_masked }}）</span>
          </span>
          <button @click="testLlm" :disabled="testing">
            {{ testing ? '请求中…' : '测试连接' }}
          </button>
        </div>
        <div v-if="testResult" :class="testResult.ok ? 'test-ok' : 'test-fail'">
          <template v-if="testResult.ok">
            ✓ 连通正常，往返 {{ testResult.latency_ms }}ms，模型返回「{{ testResult.reply }}」
          </template>
          <template v-else>
            ✗ 失败：{{ testResult.error }}
          </template>
        </div>
      </div>

      <div v-if="error" class="error">{{ error }}</div>
      <router-view />
    </main>
  </div>
</template>
