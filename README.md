# ai-ops-agent

**轻量级 AI 智能体故障排查工具**：统一接入指标、日志、链路与集群事件，由多个诊断 Agent 协同定位根因，自动分级告警，并把人工确认过的经验沉淀进本地知识库，通过 RAG 越用越准。

<p>
<img alt="release" src="https://img.shields.io/github/v/release/obc19/ai-ops-agent">
<img alt="ci" src="https://img.shields.io/github/actions/workflow/status/obc19/ai-ops-agent/ci.yml?branch=main">
<img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
<img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

- 仓库：<https://github.com/obc19/ai-ops-agent>　｜　协议：[MIT](LICENSE)
- 镜像：`ghcr.io/obc19/ai-ops-agent:0.4.0`（公开，支持 linux/amd64 与 linux/arm64，压缩后约 54MB）
- 完整使用手册：[docs/USAGE.md](docs/USAGE.md)

---

## 这是什么

线上故障发生时，值班同学要在指标、日志、链路、集群事件之间来回跳，凭经验拼出根因，再写成告警。这套动作重复、耗时、且严重依赖个人经验。

ai-ops-agent 把它自动化：**只读采集 → 异常检测 → 多 Agent 定位根因 → 分级告警 → 人工确认后沉淀经验**。它本身不采集数据、不替代任何监控组件，而是站在 Prometheus / Elasticsearch / Jaeger / Kubernetes 之上做判断与归纳。

默认 **mock 模式**，不接任何外部系统就能跑通全链路，方便先看效果再落地。

> [!IMPORTANT]
> **这个项目的「AI」是真的在调大模型。** 诊断、根因推断、报告撰写都走 LLM 推理。
> 但模型密钥必须由你自己提供——它是计费凭证，不能随镜像分发。
> **不填 `LLM_API_KEY` 时系统会退化成「规则模式」**：链路照样跑通、告警照样发，但结论是命中固定条件后套模板，没有推理能力。
> 为了让这件事不会被忽略，未配置时控制台顶部会有一条红色横幅、启动日志会打醒目告警、`/healthz` 也会返回 `llm.configured=false`。


---

## 主要特性

**接入与检测**

- 数据源全外接：Prometheus / ELK(Elasticsearch) / Jaeger / Kubernetes 只读接入，未配置的源自动跳过
- 多集群：一份 YAML 描述多个集群，事件与告警天然带集群维度
- 四级检测：静态阈值、环比激增、3σ 滑动基线、日志关键词与正则（规则在 `app/detection/rules.yaml`）

**诊断**

- 四类 Agent：指标 / 日志 / 链路 / 根因，每条结论都带「置信度 + 依据」
- 编排中枢负责超时控制（单 Agent 12s、总预算 45s）与失败降级，保证单次诊断 ≤60s
- 模型层只认 `base_url + model + key`，DeepSeek / 通义 / Kimi / 智谱 / OpenAI / 自建 vLLM 皆可
- 内置连通性探针：控制台点「测试连接」或 `POST /api/v1/system/llm-test`，一次真实请求验证密钥、网络、模型名

**安全**

- 脱敏网关只在外发前动数据：识别 IP / Token / 账号 / 主机名，命中敏感内容则**脱敏后**再外发，审计记录原文与脱敏后预览
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
                                 │
                          脱敏后内容 → 外部 LLM（OpenAI 兼容）
                                 │
                     分级告警 P0/P1/P2 → 飞书 / 钉钉 / 邮件 / Webhook
                                          │ 人工确认
                            经验入本地向量库 → 反哺下一次诊断
```

单个实例即可覆盖整个集群：采集是只读拉取，一次查询天然是全局视角，无需在每台机器上部署。

---

## 快速开始

### 0. 先配模型（唯一必做的一步）

没有它，后面几步照样能跑，但你看到的是规则模板，不是 AI 分析。

密钥走环境变量（不进文件、不进镜像），其余设置都在 `config/aiops.yaml`：

```bash
cp .env.example .env
# 编辑 .env，填入密钥（DeepSeek 控制台申请，或任意 OpenAI 兼容服务）
#   LLM_API_KEY=sk-xxxxxxxx
# 模型名与服务地址改 config/aiops.yaml 的 llm: 段即可，不用碰代码
```

> 用 Docker Compose 时注意路径：compose 默认只读**自己所在目录**的 `.env`。
> 所以要么 `cp .env.example deploy/.env`，要么启动时显式指定 `--env-file ../.env`（下面的命令用的是后者）。

验证是否真的接上了（不用等诊断跑完）：

```bash
curl -X POST http://127.0.0.1:8080/api/v1/system/llm-test \
  -H "Content-Type: application/json"
# {"configured":true,"model":"deepseek-chat","ok":true,"latency_ms":780,"reply":"模型可用",...}
```

`ok:false` 时 `error` 字段会直接说是密钥错（401）、模型名错（404）、还是网络不通。

### 1. Docker Compose（推荐）

```bash
git clone https://github.com/obc19/ai-ops-agent.git
cd ai-ops-agent/deploy
docker compose --env-file ../.env up -d --build   # .env 在仓库根目录，必须显式指定
# 打开 http://127.0.0.1:8080
```

### 2. 单容器

```bash
docker run -d --name ai-ops-agent -p 8080:8080 \
  -e MOCK_MODE=true \
  -e LLM_API_KEY=sk-xxxxxxxx \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_MODEL=deepseek-chat \
  -v $(pwd)/data:/data \
  ghcr.io/obc19/ai-ops-agent:latest
```

### 3. Kubernetes

```bash
kubectl apply -f deploy/k8s/00-namespace-rbac.yaml
kubectl -n aiops create secret generic ai-ops-agent-secrets \
  --from-literal=llm-api-key='sk-xxxxxxxx' \
  --from-literal=operator-token='<token>' --from-literal=viewer-token='<token>'
kubectl apply -f deploy/k8s/
kubectl -n aiops port-forward svc/ai-ops-agent 8080:80
```

### 4. 源码（本地开发）

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r deploy/requirements.txt
cp .env.example .env                              # 填 LLM_API_KEY
python -m app.main
```

### 确认跑的是 AI 而不是规则

诊断详情页报告末尾会写 `LLM 参与：是/否`；审计页能看到 `llm_egress` 记录（模型名、脱敏命中项、往返内容预览）。

```bash
curl "http://127.0.0.1:8080/api/v1/audit?egress_only=true"
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

更多场景（接真实 Prometheus、Alertmanager 接入、飞书加签、多集群、鉴权、模型配置与排错）见 **[docs/USAGE.md](docs/USAGE.md#6-常见使用示例)**。

---

## 配置：挂载一个目录就够了

所有可变项都在仓库根目录的 `config/` 里，运行时整目录挂载到 `/etc/aiops`：

```bash
docker run -v $(pwd)/config:/etc/aiops:ro ... ghcr.io/obc19/ai-ops-agent:latest
```

```
config/
├── aiops.yaml              # 主配置：运行模式 / 数据源 / 模型 / 告警 / 鉴权 / 超时
├── rules-detection.yaml    # 检测规则：阈值、环比、3σ 基线、日志正则
├── rules-desensitize.yaml  # 脱敏规则：IP、Token、账号、主机名
└── clusters.example.yaml   # 多集群模板，复制为 clusters.yaml 才生效
```

优先级：**环境变量 > `.env` > `config/aiops.yaml` > 代码默认值**。
所以「文件里写常态、环境变量做临时覆盖」两套都成立，老部署方式不会失效。
字段含义与完整示例见 [USAGE.md 第 4 章](docs/USAGE.md#4-配置文件挂载--环境变量覆盖)。

| 常改的项 | 位置 |
| --- | --- |
| `LLM_API_KEY` | 建议环境变量（密钥不进文件），模型名/地址在 `llm:` |
| 数据源地址 | `sources:` 段 |
| 检测周期、告警冷却 | `detection:` 段 |
| 告警渠道 Webhook | `alerting:` 段 |
| 三角色 Token | `auth:` 段或环境变量 |

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_MODE` | `true` | 用内置 mock 数据源，无需外部依赖 |
| `PORT` / `DATA_DIR` | `8080` / `./data` | 服务端口与数据目录（SQLite、向量库、审计） |
| `PROMETHEUS_URL` / `ELASTICSEARCH_URL` / `JAEGER_URL` | 空 | 数据源地址，留空则该适配器跳过 |
| `ENABLE_TRACE_AGENT` / `ENABLE_K8S_ADAPTER` | `false` | 链路 / K8s 适配器开关 |
| `LLM_API_KEY` | 空 | **必填**。不填则退化为规则模式（无 AI 推理） |
| `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek | 任意 OpenAI 兼容服务 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | 空 | 经验向量化，留空用哈希向量兜底 |
| `KNOWLEDGE_BACKEND` | `simple` | `simple` / `chroma` / `faiss` |
| `DETECT_INTERVAL` | `10` | 轮询周期（秒），决定检测延迟 |
| `ALERT_CHANNELS` | 四渠道全开 | 逗号分隔，如 `feishu,webhook` |
| `CLUSTERS_CONFIG` | 空 | 多集群 YAML 路径或内联内容 |
| `AUTH_TOKEN` / `OPERATOR_TOKEN` / `VIEWER_TOKEN` | 空 | 三角色 Token，全空则本地体验模式 |

---

## 项目结构

```
config/                     # 唯一配置源（挂载到 /etc/aiops 即可整体替换）
├── aiops.yaml              #   主配置
├── rules-detection.yaml    #   检测规则
├── rules-desensitize.yaml  #   脱敏规则
└── clusters.example.yaml   #   多集群模板

app/                        # 运行时程序，不含任何配置值
├── main.py                 #   FastAPI 入口（API + 静态控制台 + 健康检查）
├── config/                 #   Pydantic 配置、YAML 配置源、常量、多集群加载
├── models/                 #   事件 / 诊断 / 告警 / 经验 / 集群模型
├── collectors/             #   采集适配器（Prometheus / ES / Jaeger / K8s）
├── detection/              #   异常检测（阈值 / 基线 / 日志规则）
├── gateway/                #   数据安全网关（脱敏 + 分流路由 + 审计）
├── agents/                 #   四类 Agent + 编排中枢 + 报告渲染
├── alerting/               #   告警策略 / 聚合升级 / 多渠道通知
├── knowledge/              #   向量库 / RAG / 经验流转
├── storage/                #   SQLite 仓储
├── api/                    #   REST 路由
├── cli/                    #   命令行工具 ops-cli
├── mock/                   #   mock 世界（可注入故障）
├── services/               #   诊断闭环流水线
└── scheduler.py            #   轮询检测调度

frontend/                   # Vue 3 + TS 控制台（构建产物打进镜像）
deploy/                     # 构建与部署：Dockerfile / compose / k8s 清单 / requirements.txt
docs/                       # 使用手册、变更日志、贡献指南、安全说明
tools/                      # 开发脚本：demo.py 端到端自证（不打进镜像）
tests/                      # pytest
```

**仓库根目录只留门面**：`README.md`、`LICENSE`、`Makefile`、`pyproject.toml`（打包元数据，必须在根）、`.env.example`。
文档进 `docs/`、依赖清单进 `deploy/`、脚本进 `tools/`，运行时镜像只 COPY `app/` + `config/` + 前端产物。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/USAGE.md](docs/USAGE.md) | **完整使用手册**：环境依赖、四种安装方式、全部配置项、控制台/API/CLI 用法、8 个典型场景、运维与故障排查 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 版本变更记录 |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | 开发环境、代码规范、提交约定 |
| [docs/SECURITY.md](docs/SECURITY.md) | 漏洞报告方式与安全边界 |
| [.env.example](.env.example) | 全部环境变量模板，复制为 `.env` 即可用 |
| [config/aiops.yaml](config/aiops.yaml) | 主配置模板，全部字段带注释 |
| [config/clusters.example.yaml](config/clusters.example.yaml) | 多集群配置示例 |

---

## 已知边界

- **诊断不自动处置**：只给结论与建议，重启、扩缩容等写操作需人工执行
- **单副本运行**：进程内调度 + SQLite 单写，多副本会重复检测、重复告警、分裂知识库
- **Agent 为单轮推理**：规则与 LLM 双路径，尚未做多轮工具调用与自主规划
- **模型密钥需自带**：镜像不含任何密钥，未配置时降级为规则模式（横幅与 `/healthz` 会明示）
- **链路 / K8s 适配器默认关闭**：需显式开启对应开关
- **内嵌向量库适合中小规模**：经验上万条建议切 `KNOWLEDGE_BACKEND=chroma`
- **报告不产出 PDF**：提供 JSON 与 Markdown（`GET /api/v1/diagnostics/{id}/markdown`）

## 路线图

- [ ] 自动处置 / 自愈（重启、扩缩容）
- [ ] 告警降噪的 AI 关联分析
- [ ] 多租户与细粒度 RBAC
- [ ] Helm Chart 与多副本部署
- [ ] 插件市场与社区采集器生态
- [ ] 多轮工具调用（让 Agent 自己查指标，而不是一次性喂上下文）

## 许可

[MIT](LICENSE)
