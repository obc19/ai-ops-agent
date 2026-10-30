# ai-ops-agent

**轻量级 AI 智能体故障排查工具**：统一接入指标、日志、链路与集群事件，由多个诊断 Agent 协同定位根因，自动分级告警，并把人工确认过的经验沉淀进本地知识库，通过 RAG 越用越准。

<p>
<img alt="release" src="https://img.shields.io/github/v/release/obc19/ai-ops-agent">
<img alt="ci" src="https://img.shields.io/github/actions/workflow/status/obc19/ai-ops-agent/ci.yml?branch=main">
<img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
<img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

- 仓库：<https://github.com/obc19/ai-ops-agent>　｜　协议：[MIT](LICENSE)
- 镜像：`ghcr.io/obc19/ai-ops-agent:0.6.0`（公开，支持 linux/amd64 与 linux/arm64，压缩后约 54MB）
- 完整使用手册：[docs/USAGE.md](docs/USAGE.md)

---

## 这是什么

线上故障发生时，值班同学要在指标、日志、链路、集群事件之间来回跳，凭经验拼出根因，再写成告警。这套动作重复、耗时、且严重依赖个人经验。

ai-ops-agent 把它自动化：**监测服务器状态 → 异常检测 → 多 Agent 定位根因 → 给出可执行修复方案 → 分级告警 → 人工确认后沉淀经验**。

它自带三类「一定有信号」的数据源，不依赖任何外部监控组件：

- **服务器本体**：直读 `/proc`，采集负载、CPU、IO 等待、内存、Swap、磁盘、fd、连接数、僵尸进程
- **主动拨测**：自己定时访问你配置的 URL，把一次访问拆成 DNS / 建立连接 / TLS / 首字节 / 总耗时五段
- **K8s 原生**：直读集群 API，采集 Pod 重启 / 未就绪 / 容器失败原因、节点 Ready 与资源压力、Deployment 副本缺口、Warning 事件

其余数据源（Prometheus / Elasticsearch / Jaeger）只读外接，未配置的自动跳过。

> **没有 Prometheus、也没有 ELK 能用吗？** 能。上面三类都是零依赖的，
> K8s 那一路只需要一个 ServiceAccount（RBAC 清单已给），装不装 metrics-server 都能工作——
> 装了多一条 CPU/内存用量曲线，没装就退化成「事件 + 重启次数 + 就绪状态 + 副本缺口」，
> 控制台会明确标注当前处于哪一档。详见 [无 Prometheus / 无 ELK 模式](#无-prometheus--无-elk-模式)。

默认 **mock 模式**，不接任何外部系统就能跑通全链路，方便先看效果再落地。

> [!IMPORTANT]
> **这个项目的「AI」是真的在调大模型。** 诊断、根因推断、报告撰写都走 LLM 推理。
> 但模型密钥必须由你自己提供——它是计费凭证，不能随镜像分发。
> **不填 `LLM_API_KEY` 时系统会退化成「规则模式」**：链路照样跑通、告警照样发，但结论是命中固定条件后套模板，没有推理能力。
> 为了让这件事不会被忽略，未配置时控制台顶部会有一条红色横幅、启动日志会打醒目告警、`/healthz` 也会返回 `llm.configured=false`。


---

## 主要特性

**接入与检测**

- **服务器本体**：直读 `/proc`，零外部依赖，覆盖负载、CPU、IO 等待、内存、Swap、磁盘、fd、连接数、僵尸进程
- **主动拨测**：自己访问目标 URL，分五段计时（DNS / 建连 / TLS / TTFB / 总耗时），直接回答「慢在哪一段」
- **K8s 原生信号**：只读集群 API 拿 Pod 重启次数、容器失败原因（CrashLoopBackOff / OOMKilled / ImagePullBackOff）、
  节点 Ready 与 MemoryPressure/DiskPressure、Deployment 副本缺口、Warning 事件；集群内运行零配置自动启用，
  metrics-server 存在时自动补 CPU/内存用量，不存在则静默降级
- 其余数据源只读外接：Prometheus / Elasticsearch / Jaeger，未配置的源自动跳过
- 多集群：一份 YAML 描述多个集群，事件与告警天然带集群维度
- 四级检测：静态阈值、环比激增、3σ 滑动基线、日志关键词与正则（规则在 `config/rules-detection.yaml`）

**诊断**

- **服务依赖拓扑**：从 Jaeger span 的父子关系自动推导调用图，也可在配置里手工声明兜底
- **根因收敛**：多个服务同时异常时按拓扑判定源头，**N 条告警压成 1 条**
  （根因 + 受影响列表），无拓扑数据时退化为不收敛，绝不硬并
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
                异常检测（阈值 / 环比 / 3σ / 日志规则 / K8s 事件规则）
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

## 无 Prometheus / 无 ELK 模式

很多集群只有 K8s 本身，没上监控栈。下面这张表说明**每一层信号从哪来、缺了会失去什么**，
这样你能判断哪些故障还能被定位、哪些会瞎。

| 信号 | 来源 | 需要装什么 | 失去它意味着 |
| --- | --- | --- | --- |
| 服务器负载 / CPU / 内存 / 磁盘 / fd / 连接数 / 僵尸进程 | 直读 `/proc` | 无 | — |
| 用户视角的慢（DNS / 建连 / TLS / TTFB） | 平台自己发起 HTTP 拨测 | 无（填 `config/probes.yaml`） | 无法区分「服务慢」和「网络慢」 |
| Pod 重启次数、容器失败原因、Ready 状态 | `GET /api/v1/pods` | 无 | 无法判断 CrashLoop / OOM 循环 |
| 节点 Ready、MemoryPressure / DiskPressure | `GET /api/v1/nodes` | 无 | 无法判断故障在节点还是业务 |
| Deployment 期望副本 vs 就绪副本 | `GET /apis/apps/v1/deployments` | 无 | 无法判断「服务能力掉了多少」 |
| Warning 事件（FailedScheduling / Evicted / Unhealthy） | `GET /api/v1/events` | 无 | 少一块最直接的现场描述 |
| Pod / Node 的 CPU、内存用量 | `metrics.k8s.io` | **metrics-server**（K8s 标准附加组件，`kubectl top` 依赖它） | 只能定性「在重启」，说不出「快撞 limit 了」 |
| 业务指标（QPS / 延迟 / 错误率）、JVM / 中间件指标 | Prometheus 或任何兼容查询的端点 | Prometheus（或 VictoriaMetrics 等） | 无法从业务视角发现「没报错但变慢了」 |
| 应用日志与日志规则 | Elasticsearch / OpenSearch | ELK / EFK | 4 类日志规则（超时 / OOM / 5xx / 磁盘）全部失效 |
| 服务调用拓扑 | Jaeger 或 `topology.dependencies` 手工声明 | Jaeger（可选手工兜底） | 多服务同时异常时无法收敛成单一根因 |

**关键点：故障定位的主干并不依赖监控栈。** Pod 为什么起不来、节点为什么不可用、
副本为什么掉了 —— 这些 K8s API 自己就能回答，Prometheus 和 ELK 反而拿不到
（ELK 看不到 `OOMKilled`，因为容器已经死了，日志还没落盘）。

开启方式（集群内零配置）：

```bash
# 部署到 K8s 里时，适配器检测到 ServiceAccount 会自动启用，无需改配置
kubectl apply -f deploy/k8s/00-namespace-rbac.yaml   # 只读 RBAC，无任何写权限
# 想限定只看某些命名空间：
#   config/aiops.yaml → sources.kubernetes_namespaces: "prod,web"
```

集群外运行则显式配置：

```bash
export KUBERNETES_API_URL=https://k8s.example.com:6443
export KUBERNETES_TOKEN=<只读 ServiceAccount 的 token>
export ENABLE_K8S_ADAPTER=true
```

控制台「K8s 集群状态」卡片会直接标出当前档位：
`metrics-server 可用（含 CPU/内存用量）` 或 `未安装（事件与状态不受影响）`，
不会用 0 或者空图冒充「一切正常」。同一份数据也在 `GET /api/v1/cluster`。

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
  -e LLM_API_KEY=sk-xxxxxxxx \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_MODEL=deepseek-chat \
  -v $(pwd)/data:/data \
  -v $(pwd)/config:/etc/aiops:ro \
  -v /proc:/host/proc:ro \
  -e SERVER_PROC_ROOT=/host/proc \
  ghcr.io/obc19/ai-ops-agent:latest
```

> **`-v /proc:/host/proc:ro` 是「监测服务器状态」的关键。**
> 不挂它，平台只能看到容器自己的负载与内存，节点真实状态是看不到的。
> 想同时看节点磁盘，再加 `-v /:/host/root:ro -e SERVER_DISK_PATHS=/host/root`。
> Docker Desktop（Windows / macOS）里 `/proc` 属于虚拟机，不是你的宿主系统，此时这项指标意义有限。

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
├── aiops.yaml              # 主配置：运行模式 / 数据源 / 服务器 / 模型 / 告警 / 鉴权 / 超时
├── rules-detection.yaml    # 检测规则：阈值、环比、3σ 基线、日志正则（含服务器层与拨测层）
├── rules-desensitize.yaml  # 脱敏规则：IP、Token、账号、主机名
├── probes.yaml             # 主动拨测目标：要监控的 URL 列表
└── clusters.example.yaml   # 多集群模板，复制为 clusters.yaml 才生效
```

优先级：**环境变量 > `.env` > `config/aiops.yaml` > 代码默认值**。
所以「文件里写常态、环境变量做临时覆盖」两套都成立，老部署方式不会失效。
字段含义与完整示例见 [USAGE.md 第 4 章](docs/USAGE.md#4-配置文件挂载--环境变量覆盖)。

| 常改的项 | 位置 |
| --- | --- |
| `LLM_API_KEY` | 建议环境变量（密钥不进文件），模型名/地址在 `llm:` |
| 数据源地址 | `sources:` 段 |
| 宿主机 `/proc` 路径、磁盘路径 | `server:` 段 |
| 要拨测的 URL | `probes.yaml` 的 `targets:` |
| 检测周期、告警冷却 | `detection:` 段 |
| 检测阈值（含服务器层） | `rules-detection.yaml` |
| 告警渠道 Webhook | `alerting:` 段 |
| 三角色 Token | `auth:` 段或环境变量 |

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_MODE` | `true` | 用内置 mock 数据源，无需外部依赖 |
| `PORT` / `DATA_DIR` | `8080` / `./data` | 服务端口与数据目录（SQLite、向量库、审计） |
| `SERVER_ENABLED` | `true` | 服务器本体采集开关 |
| `SERVER_PROC_ROOT` | `/proc` | 读哪个 `/proc`；容器内看宿主机填 `/host/proc` |
| `SERVER_DISK_PATHS` | `/` | 磁盘使用率路径，逗号分隔 |
| `PROBE_ENABLED` / `PROBE_CONFIG` | `true` / 空 | 拨测开关与目标文件路径 |
| `PROMETHEUS_URL` / `ELASTICSEARCH_URL` / `JAEGER_URL` | 空 | 数据源地址，留空则该适配器跳过（**都不是必需**） |
| `ENABLE_K8S_ADAPTER` | `false` | K8s 采集开关；集群内检测到 ServiceAccount、或配了 `KUBERNETES_API_URL` 时**自动启用**，无需设置 |
| `KUBERNETES_API_URL` / `KUBERNETES_TOKEN` | 空 | 集群外运行时的 API 地址与只读 token |
| `KUBERNETES_NAMESPACES` | 空 | 只看这些命名空间的 Pod/Deployment，逗号分隔；留空=全部 |
| `ENABLE_TRACE_AGENT` | `false` | 链路 Agent 开关（需要 Jaeger 或手工拓扑） |
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
├── collectors/             #   采集适配器（Prometheus / ES / Jaeger / K8s / 服务器 / 拨测）
├── detection/              #   异常检测（阈值 / 基线 / 日志规则 / K8s 事件规则）
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

- **诊断不自动处置**：只给结论与可执行步骤，重启、扩缩容等写操作由管理员执行
- **收敛依赖拓扑质量**：拓扑来自 span 父子关系，采样率过低或有大量异步调用（MQ、定时任务）时会漏边，
  需要手工补 `topology.dependencies`；没有拓扑时不会猜测
- **服务器监测依赖 `/proc`**：Linux 才有；容器需挂载宿主机 `/proc`，Docker Desktop 下只能看到虚拟机
- **看不到客户端侧**：拨测覆盖 DNS / 建连 / TLS / 首字节，但前端渲染、CDN 命中、真实用户分布不在范围内
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
