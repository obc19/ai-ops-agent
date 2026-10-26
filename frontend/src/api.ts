const TOKEN_KEY = 'aiops_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(value: string) {
  if (value) localStorage.setItem(TOKEN_KEY, value)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const resp = await fetch(path, { ...init, headers: { ...headers, ...(init.headers || {}) } })
  if (resp.status === 401) throw new Error('鉴权失败：请在右上角填入 AUTH_TOKEN')
  if (!resp.ok) throw new Error(`请求失败 HTTP ${resp.status}`)
  const type = resp.headers.get('content-type') || ''
  if (type.includes('application/json')) return (await resp.json()) as T
  return (await resp.text()) as unknown as T
}

export const api = {
  health: () => request<any>('/healthz'),
  overview: () => request<any>('/api/v1/overview'),
  alerts: (params: Record<string, any> = {}) =>
    request<any>('/api/v1/alerts?' + new URLSearchParams(params as any).toString()),
  alertMarkdown: (id: string) => request<string>(`/api/v1/alerts/${id}/markdown`),
  diagnoses: (params: Record<string, any> = {}) =>
    request<any[]>('/api/v1/diagnostics?' + new URLSearchParams(params as any).toString()),
  diagnosisMarkdown: (id: string) => request<string>(`/api/v1/diagnostics/${id}/markdown`),
  knowledge: (params: Record<string, any> = {}) =>
    request<any[]>('/api/v1/knowledge?' + new URLSearchParams(params as any).toString()),
  knowledgeStats: () => request<any>('/api/v1/knowledge/stats'),
  audit: (params: Record<string, any> = {}) =>
    request<any>('/api/v1/audit?' + new URLSearchParams(params as any).toString())
}

export function fmtTime(value?: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

const STATUS_LABELS: Record<string, string> = {
  triggered: '已触发',
  notified: '已通知',
  confirmed: '处置中',
  resolved: '已关闭'
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status
}

export function severityClass(severity: string): string {
  return severity === 'P0' ? 'tag p0' : severity === 'P1' ? 'tag p1' : 'tag p2'
}
