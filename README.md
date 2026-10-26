# ai-ops-agent

**轻量级 AI 智能体故障排查工具**：统一接入指标、日志、链路与集群事件，由多个诊断 Agent 协同定位根因，自动分级告警，并把人工确认过的经验沉淀进本地知识库，通过 RAG 越用越准。

<p>
<img alt="release" src="https://img.shields.io/github/v/release/obc19/ai-ops-agent">
<img alt="ci" src="https://img.shields.io/github/actions/workflow/status/obc19/ai-ops-agent/ci.yml?branch=main">
<img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
<img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

- 仓库：<https://github.com/obc19/ai-ops-agent>　｜　协议：[MIT](LICENSE)
- 镜像：`ghcr.io/obc19/ai-ops-agent:0.2.0`（公开，支持 linux/amd64 与 linux/arm64，压缩后约 54MB）
- 完整使用手册：[USAGE.md](USAGE.md)

---

## 这是什么

线上故障发生时，值班同学要在指标、日志、链路、集群事件之间来回跳，凭经验拼出根因，再写成告警。这套动作重复、耗时、且严重依赖个人经验。

ai-ops-agent 把它自动化：**只读采集 → 异常检测 → 多 Agent 定位根因 → 分级告警 → 人工确认后沉淀经验**。它本身不采集数据、不替代任何监控组件，而是站在 Prometheus / Elasticsearch / Jaeger / Kubernetes 之上做判断与归纳。

默认 **mock 模式**，不接任何外部系统就能跑通全链路，方便先看效果再落地。

---

## 主要特性

**接入与检测**

- 数据源全外接：Prometheus / ELK(Elasticsearch) / Jaeger / Kubernetes 只读接入，未配置的源自动跳过
- 多集群：一份 YAML 描述多个集群，事件与告警天然带集群维度
- 四级检测：静态阈值、环比激增、3σ 滑动基线、日志关键词与正则（规则在 `app/detection/rules.yaml`）

**诊断**

- 四类 Agent：指标 / 日志 / 链路 / 根因，每条结论都带「置信度 + 依据」
- 编排中枢负责超时控制（单 Agent 12s、总预算 45s）与失败降级，保证单次诊断 ≤60s
- LLM 可选：配了 `LLM_API_KEY` 走模型推理，不配则走规则引擎，功能不中断

**安全**

- 脱敏网关只在外发前动数据：识别 IP / Token / 账号 / 主机名，敏感内容可路由到本地模型
- 脱敏失败 **fail-closed**：规则异常或残留敏感值未清干净时直接拦截，并生成 P1 自告警
- 三角色静态鉴权（viewer / operator / admin），审计日志全覆盖，`egress_only` 可只看数据外发

**告警与闭环**

- P0 / P1 / P2 分级，飞书 / 钉钉（均支持加签）/ 邮件 / Webhook 四渠道
- 同对象同指标聚合，P0 不抑制可升级；状态机「触发 → 通知 → 处置中 → 已关闭」全程留痕，支持复发重开
- `POST /ingest/alertmanager` 直接消费 Alertmanager Webhook，复用同一套诊断与知识库
- 经验「草稿 → 人工确认 → 入库 → 语义检索 → 可回滚」，入库前先脱敏

**交付形态**

- 单镜像 all-in-one：后端 + 控制台 + 调度器同一进程，同源同端口，无额外 nginx
- 控制台：Vue 3 + TS，集群总览 / 告警 / 诊断报告 / 知识库 / 审计
- 一条命令起步：`docker compose up -d`，默认 mock 模式开箱即用

---

## 架构

```
Prometheus   Elasticsearch   Jaeger   Kubernetes
     │             │           │          │   只读 API 查询
     └─────────────┴───────────┴──────────┘
                          │
                采集适配器（插件式，无状态）
                          │  标准化事件 Metric / Log / Trace / K8s
                异常检测（阈值 / 环比 / 3σ / 日志规则）
                          │
          编排中枢 Orchestrator（12s per Agent，45s 预算）
        ┌─────────────┬─────────────┬─────────────┐
     指标 Agent     日志 Agent     链路 Agent
        └─────────────┴─────────────┴──────┬──────┘
                                          │ 诊断片段（置信度 + 依据）
                            根因 Agent + RAG 检索历史经验
                                          │
                  脱敏网关（外发前脱敏；失败拦截）
                          │                        │
                   非敏感 → 外部 LLM        敏感 → 本地模型
                                          │
                     分级告警 P0/P1/P2 → 飞书 / 钉钉 / 邮件 / Webhook
                                          │ 人工确认
                            经验入本地向量库 → 反哺下一次诊断
```

单个实例即可覆盖整个集群：采集是只读拉取，一次查询天然是全局视角，无需在每台机器上部署。

---

## 快速开始

### 1. Docker Compose（推荐）

```bash
git clone https://github.com/obc19/ai-ops-agent.git
cd ai-ops-agent/deploy
docker compose up -d --build
# 打开 http://127.0.0.1:8080
```

### 2. 单容器

```bash
docker run -d --name ai-ops-agent -p 8080:8080 \
  -e MOCK_MODE=true \
  -v $(pwd)/data:/data \
  ghcr.io/obc19/ai-ops-agent:latest
```

### 3. Kubernetes

```bash
kubectl apply -f deploy/k8s/00-namespace-rbac.yaml
kubectl -n aiops create secret generic ai-ops-agent-secrets \
  --from-literal=operator-token='<token>' --from-literal=viewer-token='<token>'
kubectl apply -f deploy/k8s/
kubectl -n aiops port-forward svc/ai-ops-agent 8080:80
```

### 4. 源码（本地开发）

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
MOCK_MODE=true PORT=8080 python -m app.main
```

### 看一眼完整闭环

注入故障 → 自动检测诊断 → 分级告警 → 确认入库：

```bash
curl -X POST http://127.0.0.1:8080/_mock/fault \
  -H "Content-Type: application/json" -d '{"target":"order-service","severity":"P0"}'

curl -X POST http://127.0.0.1:8080/api/v1/diagnostics/detect \
  -H "Content-Type: application/json" -d '{"notify":true}'

ops-cli alerts && ops-cli report <诊断ID>
```

更多场景（接真实 Prometheus、Alertmanager 接入、飞书加签、多集群、鉴权、本地模型）见 **[USAGE.md](USAGE.md#6-常见使用示例)**。

---

## 配置速查

全部通过环境变量注入（支持 `.env`），完整清单见 [USAGE.md 第 4 章](USAGE.md#4-配置参考) 与 `.env.example`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_MODE` | `true` | 用内置 mock 数据源，无需外部依赖 |
| `PORT` / `DATA_DIR` | `8080` / `./data` | 服务端口与数据目录（SQLite、向量库、审计） |
| `PROMETHEUS_URL` / `ELASTICSEARCH_URL` / `JAEGER_URL` | 空 | 数据源地址，留空则该适配器跳过 |
| `ENABLE_TRACE_AGENT` / `ENABLE_K8S_ADAPTER` | `false` | 链路 / K8s 适配器开关 |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | DeepSeek | 诊断使用的模型（OpenAI 兼容），不配则全部走规则 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | 空 | 经验向量化，留空用哈希向量兜底 |
| `KNOWLEDGE_BACKEND` | `simple` | `simple` / `chroma` / `faiss` |
| `DETECT_INTERVAL` | `10` | 轮询周期（秒），决定检测延迟 |
| `ALERT_CHANNELS` | 四渠道全开 | 逗号分隔，如 `feishu,webhook` |
| `CLUSTERS_CONFIG` | 空 | 多集群 YAML 路径或内联内容 |
| `AUTH_TOKEN` / `OPERATOR_TOKEN` / `VIEWER_TOKEN` | 空 | 三角色 Token，全空则本地体验模式 |

---

## 项目结构

```
app/
├── main.py                 # FastAPI 入口（API + 静态控制台 + 健康检查）
├── config/                 # Pydantic 配置、常量、多集群加载
├── models/                 # 事件 / 诊断 / 告警 / 经验 / 集群模型
├── collectors/             # 采集适配器（Prometheus / ES / Jaeger / K8s）
├── detection/              # 异常检测（rules.yaml + 阈值 / 基线 / 日志规则）
├── gateway/                # 数据安全网关（脱敏规则 + 分流路由 + 审计）
├── agents/                 # 四类 Agent + 编排中枢 + 报告渲染
├── alerting/               # 告警策略 / 聚合升级 / 多渠道通知
├── knowledge/              # 向量库 / RAG / 经验流转
├── storage/                # SQLite 仓储
├── api/                    # REST 路由
├── cli/                    # 命令行工具 ops-cli
├── mock/                   # mock 世界（可注入故障）
├── services/               # 诊断闭环流水线
└── scheduler.py            # 轮询检测调度
frontend/                   # Vue 3 + TS 控制台（构建产物打进镜像）
deploy/                     # Dockerfile / docker-compose / k8s 清单 / 多集群示例
scripts/demo.py             # 端到端验收自证
tests/                      # pytest
```

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [USAGE.md](USAGE.md) | **完整使用手册**：环境依赖、四种安装方式、全部配置项、控制台/API/CLI 用法、8 个典型场景、运维与故障排查 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、代码规范、提交约定 |
| [SECURITY.md](SECURITY.md) | 漏洞报告方式与安全边界 |
| [.env.example](.env.example) | 全部环境变量模板，复制为 `.env` 即可用 |
| [deploy/clusters.example.yaml](deploy/clusters.example.yaml) | 多集群配置示例 |

---

## 已知边界

- **诊断不自动处置**：只给结论与建议，重启、扩缩容等写操作需人工执行
- **单副本运行**：进程内调度 + SQLite 单写，多副本会重复检测、重复告警、分裂知识库
- **Agent 为单轮推理**：规则与 LLM 双路径，尚未做多轮工具调用与自主规划
- **链路 / K8s 适配器默认关闭**：需显式开启对应开关
- **内嵌向量库适合中小规模**：经验上万条建议切 `KNOWLEDGE_BACKEND=chroma`
- **报告不产出 PDF**：提供 JSON 与 Markdown（`GET /api/v1/diagnostics/{id}/markdown`）

## 路线图

- [ ] 自动处置 / 自愈（重启、扩缩容）
- [ ] 告警降噪的 AI 关联分析
- [ ] 多租户与细粒度 RBAC
- [ ] Helm Chart 与多副本部署
- [ ] 插件市场与社区采集器生态
- [ ] 本地模型推理优化（私有化部署替代外部 API 为主路径）

## 许可

[MIT](LICENSE)
