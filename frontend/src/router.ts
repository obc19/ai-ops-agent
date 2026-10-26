import { createRouter, createWebHashHistory } from 'vue-router'
import OverviewView from './views/OverviewView.vue'
import AlertsView from './views/AlertsView.vue'
import DiagnosesView from './views/DiagnosesView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import AuditView from './views/AuditView.vue'

export const router = createRouter({
  // 用 hash 路由，免去服务端 rewrite 配置
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'overview', component: OverviewView, meta: { title: '总览' } },
    { path: '/alerts', name: 'alerts', component: AlertsView, meta: { title: '告警' } },
    { path: '/diagnoses', name: 'diagnoses', component: DiagnosesView, meta: { title: '诊断' } },
    { path: '/knowledge', name: 'knowledge', component: KnowledgeView, meta: { title: '知识库' } },
    { path: '/audit', name: 'audit', component: AuditView, meta: { title: '审计' } }
  ]
})
