# ai-ops-agent

基于 Python 的**轻量级 AI 智能体故障排查工具**。统一接入指标、日志、链路与集群事件，由多个诊断 Agent 协同定位根因，自动分级告警，并把人工确认过的经验沉淀进本地知识库，通过 RAG 越用越准。

> 当前版本为 **v0.1.0 骨架版**：目录、接口、数据模型、配置体系、mock 数据源、容器化、控制台、测试与验收脚本全部到位，端到端可跑通；Agent 推理支持「规则引擎」与「LLM」两条路径，未配置 LLM 时自动走规则结论。

---

## 特性

- **一条命令起步**：`docker compose up -d` 或 `docker run`，默认 mock 模式开箱即用，无需任何外部依赖
- **数据源全外接**：Prometheus / ELK(Elasticsearch) / Jaeger / Kubernetes 只读接入，工具本体不捆绑重组件
- **四级检测**：静态阈值、环比激增、3σ 滑动基线、日志关键词/正则
- **四类 Agent**：指标 / 日志 / 链路 / 根因，产出「置信度 + 依据」，编排中枢负责超时控制与失败降级
- **脱敏网关**：外发前识别 IP / Token / 账号 / 主机名，敏感数据可路由到本地模型，脱敏失败一律拦截（fail-closed）
- **告警闭环**：P0/P1/P2 分级，飞书 / 钉钉 / 邮件 / Webhook 四渠道，同对象同指标聚合，P0 支持升级
- **经验自学习**：草稿 → 人工确认 → 入库 → 语义检索（RAG）→ 可回滚，入库前先脱敏
- **只读控制台**：Vue 3 + TS，集群总览 / 告警 / 诊断报告 / 知识库 / 审计

---

## 架构

```
外部数据源（复用已有环境，不随镜像分发）
  Prometheus    Elasticsearch(ELK)    Jaeger    Kubernetes
        │              │                │            │
        └──── 只读 API/HTTP 查询 ───────┴────────────┘
                             │
                   采集适配器层（插件式，无状态）
         prometheus / elasticsearch / jaeger / kubernetes
                             │ 标准化事件 MetricEvent / LogEvent / TraceEvent
                   异常检测引擎（阈值 / 环比 / 3σ / 日志规则）
                             │ AnomalyEvent
                   编排中枢 Orchestrator（超时 12s/Agent，总预算 45s）
        ┌──────────────┬──────────────┬──────────────┐
     指标 Agent      日志 Agent      链路 Agent
        └──────────────┴──────────────┴──────┬───────┘
                                             │ 诊断片段（置信度 + 依据）
                              根因定位 Agent + RAG 检索历史经验
                                             │
        数据安全网关（外发前脱敏 → 敏感走本地模型 / 非敏感走外部 LLM）
                                             │
                       分级告警 P0/P1/P2 → 飞书/钉钉/邮件/Webhook
                                             │ 人工确认
                              经验入本地向量库（FAISS/Chroma/内嵌）
                                             └──→ 反哺下一次诊断
```

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
cd deploy
docker compose up -d --build
# 打开 http://127.0.0.1:8080
```

### 方式二：单容器

```bash
docker run -d --name ai-ops-agent -p 8080:8080 \
  -e MOCK_MODE=true \
  -v $(pwd)/data:/data \
  ghcr.io/obc19/ai-ops-agent:latest
```

### 方式三：本地开发

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 前端（可选，构建后由 FastAPI 托管）
cd frontend && npm install && npm run build && cd ..

MOCK_MODE=true PORT=8080 python -m app.main
```

### 体验一条完整故障闭环

```bash
# 1) 注入一个 mock 故障（无真实环境时）
curl -X POST http://127.0.0.1:8080/_mock/fault \
  -H "Content-Type: application/json" \
  -d '{"target":"order-service","metric":"cpu_usage_percent"}'

# 2) 立即执行一轮检测 → 诊断 → 告警
curl -X POST http://127.0.0.1:8080/api/v1/diagnostics/detect \
  -H "Content-Type: application/json" -d '{"notify":true}'

# 3) 查看告警与诊断报告
ops-cli alerts
ops-cli report <诊断ID>

# 4) 确认告警 → 生成经验草稿 → 人工确认入库
ops-cli alert-confirm <告警ID> --by alice
curl -X POST http://127.0.0.1:8080/api/v1/knowledge \
  -H "Content-Type: application/json" -d '{"diagnosis_id":"<诊断ID>"}'
ops-cli knowledge confirm <经验ID> --by alice

# 5) 再次触发相似故障，可看到 RAG 命中历史经验
curl -X POST http://127.0.0.1:8080/_mock/reset
```

---

## 配置

全部通过环境变量注入，无需改镜像。完整清单见 `.env.example`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_MODE` | `true` | 使用内置 mock 数据源（无外部依赖即可跑通） |
| `PORT` / `DATA_DIR` | `8080` / `./data` | 服务端口与数据目录（SQLite、向量库、审计） |
| `PROMETHEUS_URL` | 空 | Prometheus 地址，留空则该适配器跳过 |
| `ELASTICSEARCH_URL` / `ELASTICSEARCH_INDEX` | 空 / `app-logs-*` | 日志检索入口 |
| `JAEGER_URL` | 空 | 链路查询入口 |
| `KUBERNETES_API_URL` / `KUBERNETES_TOKEN` | 空 | 集群事件入口 |
| `ENABLE_TRACE_AGENT` | `false` | 链路 Agent 开关（默认关闭，可插拔） |
| `ENABLE_K8S_ADAPTER` | `false` | K8s 适配器开关 |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | DeepSeek 默认 | 非敏感数据使用的外部 LLM |
| `LOCAL_MODEL_URL` / `LOCAL_MODEL_ENABLED` | 空 / `false` | 敏感数据使用的本地模型（Ollama） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 空 | OpenAI 兼容 embedding；留空则用本地哈希向量兜底 |
| `KNOWLEDGE_BACKEND` | `simple` | `simple` / `chroma` |
| `DETECT_INTERVAL` | `10` | 轮询周期（秒），决定检测延迟 |
| `RETENTION_DAYS` | `30` | 告警/诊断/审计保留天数（经验不自动删） |
| `ALERT_GROUP_WINDOW` / `ALERT_COOLDOWN` | `300` / `300` | 聚合窗口与通知冷却 |
| `AUTH_TOKEN` | 空 | 配置后所有 API 需 `Authorization: Bearer <token>` |

---

## API

Base：`/api/v1`，交互式文档：`/docs`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/overview` | 全局总览（统计、数据源健康、运行模式） |
| POST / GET | `/clusters` | 注册集群 / 集群列表 |
| GET | `/clusters/{name}/health` | 数据源健康度 |
| POST | `/diagnostics` | 手工触发一次诊断（也可用于外部告警接入） |
| POST | `/diagnostics/detect` | 立即执行一轮采集 + 检测 + 诊断 |
| GET | `/diagnostics` / `/diagnostics/{id}` | 诊断列表 / 详情（含依据与 RAG 命中） |
| GET | `/diagnostics/{id}/markdown` | 导出 Markdown 报告 |
| GET | `/alerts` | 告警列表（按级别/状态/集群过滤） |
| GET | `/alerts/{id}/markdown` | 告警 Markdown |
| POST | `/alerts/{id}/confirm` / `resolve` / `renotify` | 确认 / 关闭 / 重发 |
| GET | `/knowledge` / `/knowledge/stats` / `/knowledge/search` | 经验列表 / 统计 / 语义检索 |
| POST | `/knowledge` | 由诊断报告生成经验草稿 |
| POST | `/knowledge/{id}/confirm` | 人工确认入库（`confirmed_by` 必填） |
| POST | `/knowledge/{id}/rollback` | 回滚经验 |
| GET | `/audit` | 审计日志（`egress_only=true` 只看数据外发） |
| GET | `/healthz` | 健康检查（容器探针） |

Mock 控制面（仅 `MOCK_MODE=true` 有意义）：`POST /_mock/fault`、`POST /_mock/reset`、`GET /_mock/faults`、`GET /_mock/sink`。

---

## CLI

```bash
ops-cli --api http://127.0.0.1:8080 overview
ops-cli diagnose --target order-service --metric cpu_usage_percent --severity P1
ops-cli detect
ops-cli alerts --severity P0
ops-cli alert-confirm <告警ID> --by alice
ops-cli knowledge list --status draft
ops-cli knowledge confirm <经验ID> --by alice
ops-cli knowledge rollback <经验ID> --reason "重复经验"
ops-cli audit --egress
```

---

## 验收

```bash
python scripts/demo.py          # 或 make demo
```

脚本会自动拉起 mock LLM 与本地服务，逐条验证需求文档第 6 章：

| # | 验收项 | 判定方式 |
| --- | --- | --- |
| 1 | 三类数据源可接入可查询且可外接 | `/overview` 中各适配器 `reachable=true` |
| 2 | 模拟故障 60s 内完成诊断并分级告警 | 注入 CPU 故障 → 计时 `/diagnostics/detect` |
| 3 | `docker compose up` 一键启动 | 有 Docker 时实际构建并启动（否则 SKIP） |
| 4 | 镜像 ≤300MB、启动 ≤5s | `docker image inspect` + `/healthz` 计时（否则 SKIP） |
| 5 | 脱敏后外发不含原始敏感值 | mock LLM 捕获全部请求体并断言不含 IP/Token/邮箱 |
| 6 | 经验确认入库并被 RAG 命中 | 确认 → 再触发相似故障 → 断言 `rag_hits` 非空 |
| 7 | 至少一种渠道成功送达 | 内置接收端 `/_mock/sink` 断言四渠道 payload |
| 8 | 开源要素齐备 | LICENSE / README / CONTRIBUTING / SECURITY / CI 存在性 |

单元与接口测试：`python -m pytest -q`

---

## 目录结构

```
app/
├── main.py                 # FastAPI 入口（API + 静态控制台 + 健康检查）
├── config/                 # Pydantic 配置与常量
├── models/                 # 事件 / 诊断 / 告警 / 经验 / 集群数据模型
├── collectors/             # 采集适配器（base + 四个数据源）
├── detection/              # 异常检测（rules.yaml + 阈值/基线/日志规则）
├── gateway/                # 数据安全网关（脱敏规则 + 分流路由 + 审计）
├── agents/                 # 四个 Agent + 编排中枢 + 报告渲染
├── alerting/               # 告警策略 + 多渠道通知
├── knowledge/              # 向量库 / RAG / 经验流转
├── storage/                # SQLite 仓储
├── api/                    # REST 路由
├── cli/                    # 命令行工具
├── mock/                   # mock 世界（可注入故障）
├── services/               # 诊断闭环流水线
└── scheduler.py            # 轮询检测调度
frontend/                   # Vue 3 + TS 控制台（产物打包进镜像）
deploy/                     # Dockerfile / docker-compose.yml
scripts/demo.py             # 端到端验收自证
tests/                      # pytest
```

---

## 已知边界

- **本轮为骨架版**：Agent 以规则 + LLM 双路径实现，尚未做多轮推理与工具调用；链路/K8s 默认关闭
- **向量库默认内嵌轻量实现**：经验量上万时请切 `KNOWLEDGE_BACKEND=chroma`
- **mock 模式的通知**发往内置接收端 `/_mock/sink`，便于验收；接真实渠道只需配置对应 Webhook / SMTP
- **飞书/钉钉的正式接口签名**（加签校验）未实现，当前为通用 Webhook 形态
- **授权协议**：仓库现为 MIT；需求文档建议 Apache-2.0，如需切换请告知

## 路线图

- [ ] 自动处置 / 自愈（重启、扩缩容）
- [ ] 告警降噪的 AI 关联分析
- [ ] 多租户与细粒度 RBAC
- [ ] Helm Chart 与多副本部署
- [ ] 插件市场与社区采集器生态
- [ ] 本地模型推理优化（私有化部署替代外部 API 为主路径）

## 许可

[MIT](LICENSE)
