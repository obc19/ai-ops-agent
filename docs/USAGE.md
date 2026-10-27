# ai-ops-agent 使用文档

> 适用版本：**v0.4.0**　｜　协议：MIT　｜　仓库：<https://github.com/obc19/ai-ops-agent>

本文覆盖从零安装到日常使用的全部环节：项目能做什么、需要什么环境、怎么装、怎么配、怎么用、出问题怎么查。只想先跑起来看效果，直接跳到 [3.1 用 Docker Compose 启动](#31-用-docker-compose-启动推荐) 或 [3.4 用源码启动](#34-用源码启动本地开发)。

**目录**

1. [项目简介](#1-项目简介)
2. [环境依赖](#2-环境依赖)
3. [安装与启动](#3-安装与启动)
4. [配置：文件挂载与环境变量](#4-配置文件挂载--环境变量覆盖)
5. [基本用法](#5-基本用法)
6. [常见使用示例](#6-常见使用示例)
7. [数据、备份与运维](#7-数据备份与运维)
8. [故障排查](#8-故障排查)
9. [自测与验收](#9-自测与验收)
10. [能力边界](#10-能力边界)

---

## 1. 项目简介

### 1.1 它解决什么问题

线上出故障时，值班同学要在指标、日志、链路、集群事件四个系统之间来回跳，凭经验拼出根因，再把结论写成告警发出去。这套动作重复、耗时、且高度依赖个人经验。

ai-ops-agent 把这套动作自动化：**统一采集 → 自动检测异常 → 多 Agent 协同定位根因 → 分级告警 → 人工确认后沉淀经验**。经验入库后，下一次相似故障会通过 RAG 命中历史结论，越用越准。

### 1.2 它是什么、不是什么

| 是 | 不是 |
| --- | --- |
| 一个**消费方**工具：读取 Prometheus / Elasticsearch / Jaeger / Kubernetes 的只读 API | 不是采集器，不替代 node-exporter、fluent-bit、OTel Collector |
| 单容器应用，进程内包含采集、检测、诊断、告警、控制台 | 不是需要一整套微服务编排的平台 |
| 默认 mock 模式，无外部依赖即可跑通全链路 | 不是在 mock 模式下就能替代真实故障演练 |
| 支持自动诊断与建议，最终处置仍需人确认 | 不做自动重启 / 扩缩容等写操作 |

### 1.3 工作原理

```
Prometheus ┐
ELK/ES     ├─ 只读查询 ─→ 采集适配器 ─→ 标准化事件 ─→ 异常检测（阈值/环比/3σ/日志规则）
Jaeger     │                                                    │
K8s events ┘                                                    ▼
                                              编排中枢（超时 12s/Agent，总预算 45s）
                                     ┌──────────────┬──────────────┐
                                 指标 Agent      日志 Agent      链路 Agent
                                     └──────────────┴──────┬───────┘
                                                           ▼
                                      根因 Agent + RAG 检索历史经验
                                                           ▼
                                脱敏网关（外发前脱敏；失败一律拦截）
                                                           ▼
                                        脱敏后内容 → 外部 LLM（必配）
                                                           ▼
                                        P0/P1/P2 分级告警 → 飞书/钉钉/邮件/Webhook
                                                           ▼
                                        人工确认 → 经验草稿 → 入库 → 反哺下次诊断
```

关键设计取舍：

- **数据源全外接**：镜像里不捆绑 Prometheus / ES / LLM。换环境只改环境变量，不改代码。
- **脱敏只做外发前**：内部计算用原始数据，只有送 LLM / embedding 时才脱敏，避免内部链路丢信息。
- **脱敏失败 fail-closed**：网关规则异常或残留敏感值未清干净时，直接拦截外发并生成 P1 自告警，不冒险放行。
- **单副本运行**：采集调度与 SQLite 都在进程内，多副本会重复检测、重复告警、分裂知识库。

---

## 2. 环境依赖

### 2.1 运行方式一：Docker（推荐）

| 项 | 要求 |
| --- | --- |
| Docker | 20.10+（使用 compose 需 v2 插件） |
| 内存 | 建议分配 512MB（设计目标：空闲占用 ≤256MB） |
| 磁盘 | 镜像 <300MB（含前端产物）+ 数据目录（按 `RETENTION_DAYS` 增长） |
| 网络 | 容器需能访问数据源地址与 LLM 地址；控制台端口 8080 |

无需预装 Python、Node，前端产物已打包在镜像内。

### 2.2 运行方式二：源码

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | **3.11+** | `pyproject.toml` 已声明 `requires-python >=3.11` |
| Node.js | 20+（仅构建前端需要） | 只跑后端 API 可不装，页面会提示「前端尚未构建」 |
| 依赖包 | 见 `deploy/requirements.txt` | FastAPI / uvicorn / httpx / Pydantic v2 / PyYAML / pytest |
| 操作系统 | Linux / macOS / Windows | Windows 控制台需注意编码，见 [8. 故障排查](#8-故障排查) |

### 2.3 外部依赖（按需接入）

数据源**全部可选**，没配的模块自动跳过，不影响其他链路。但 **LLM 不是可选项**——它是这个项目叫「AI」Ops 的原因。

| 依赖 | 用途 | 相关变量 |
| --- | --- | --- |
| **外部 LLM**（DeepSeek 等 OpenAI 兼容接口） | **诊断 / 根因 / 报告撰写，必配** | `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` |
| Prometheus | 指标查询（每轮 4 个 PromQL） | `PROMETHEUS_URL` |
| Elasticsearch（ELK） | 日志检索（5 条日志规则） | `ELASTICSEARCH_URL` / `ELASTICSEARCH_INDEX` |
| Jaeger | 链路查询 | `JAEGER_URL` + `ENABLE_TRACE_AGENT=true` |
| Kubernetes apiserver | 集群事件 | K8s 内运行可自动发现；否则 `KUBERNETES_API_URL` |
| Embedding 接口 | 经验向量化 | `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` |
| 飞书 / 钉钉 / SMTP / Webhook | 告警送达 | `FEISHU_WEBHOOK` 等，见 [4.9](#49-告警渠道) |

> **不配 LLM 会发生什么（必须知道）**：Agent 全部走内置规则，结论是「命中固定条件 → 套模板」，没有推理；报告置信度天然偏低，末尾标注 `LLM 参与：否`。
> 此时链路仍然完整可跑，告警照发，但它已经不是 AI 分析了。系统会通过三种方式提醒你：
>
> 1. 启动日志打印一段带 `====` 框的 WARNING；
> 2. 控制台顶部红色横幅，写明「当前没有接入大模型」并给出配置片段；
> 3. `GET /healthz` 返回 `"llm": {"configured": false, "mode": "rules"}`。
>
> 不配 embedding 时用内置哈希向量兜底，RAG 仍可用但语义精度有限——这条影响小，可以后补。

---

## 3. 安装与启动

### 3.0 先准备模型（否则后面的「AI 分析」只是规则模板）

任选一个 OpenAI 兼容服务，拿到三样东西：`API Key`、`Base URL`、`模型名`。

| 服务 | `LLM_BASE_URL` | `LLM_MODEL` 示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 自建 vLLM / Ollama | `http://<host>:8000/v1` | 部署时的模型名 |

写好 `.env`（仓库根目录有 `.env.example` 可直接复制）：

```bash
LLM_API_KEY=sk-xxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

> 密钥只走环境变量，不进配置文件、不进镜像、不在接口里回显（`/api/v1/system/llm` 只返回打码值 `sk-x…abcd`）。

### 3.1 用 Docker Compose 启动（推荐）

```bash
git clone https://github.com/obc19/ai-ops-agent.git
cd ai-ops-agent/deploy
docker compose --env-file ../.env up -d --build   # .env 在仓库根，compose 默认只读本目录
docker compose logs -f ai-ops-agent     # 看启动日志
```

启动日志里应有一行：`模型已接入：deepseek-chat @ api.deepseek.com（密钥 sk-x…abcd）`。
如果看到的是 `====` 框住的「未检测到 LLM_API_KEY」，说明环境变量没传进容器。

打开 <http://127.0.0.1:8080> 即为控制台。默认 `MOCK_MODE=true`，无需任何数据源即可看到完整链路。

停止与清理：

```bash
docker compose down          # 停止并移除容器，数据保留在 deploy/data
```

### 3.2 用单容器启动

```bash
docker run -d --name ai-ops-agent -p 8080:8080 \
  -e MOCK_MODE=true \
  -e LLM_API_KEY=sk-xxxxxxxx \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_MODEL=deepseek-chat \
  -v $(pwd)/data:/data \
  ghcr.io/obc19/ai-ops-agent:latest

docker logs -f ai-ops-agent | head -5     # 确认「模型已接入」
```

接真实环境时把环境变量换成实际值（建议用 `--env-file .env` 传入，模板见仓库根目录 `.env.example`）：

```bash
docker run -d --name ai-ops-agent -p 8080:8080 \
  -e MOCK_MODE=false \
  -e PROMETHEUS_URL=http://prometheus.monitoring:9090 \
  -e ELASTICSEARCH_URL=http://elasticsearch.logging:9200 \
  -e LLM_API_KEY=sk-xxxx \
  -v /opt/aiops/data:/data \
  ghcr.io/obc19/ai-ops-agent:latest
```

### 3.3 部署到 Kubernetes

清单在 `deploy/k8s/`，按序号执行：

```bash
# 1) 命名空间 + 只读 ClusterRole/ServiceAccount
kubectl apply -f deploy/k8s/00-namespace-rbac.yaml

# 2) 凭据（Token 与 LLM Key 走 Secret，不写进清单）
kubectl -n aiops create secret generic ai-ops-agent-secrets \
  --from-literal=operator-token='<operator-token>' \
  --from-literal=viewer-token='<viewer-token>' \
  --from-literal=llm-api-key='<llm-api-key>'

# 3) 配置目录（仓库 config/ 整目录，不内联、不复制）
kubectl -n aiops create configmap ai-ops-agent-config \
  --from-file=config/aiops.yaml \
  --from-file=config/rules-detection.yaml \
  --from-file=config/rules-desensitize.yaml

# 4) PVC / Deployment / Service / Ingress / NetworkPolicy
kubectl apply -f deploy/k8s/10-config.yaml
kubectl apply -f deploy/k8s/20-workload.yaml
kubectl apply -f deploy/k8s/30-ingress-networkpolicy.yaml

kubectl -n aiops rollout status deploy/ai-ops-agent
kubectl -n aiops port-forward svc/ai-ops-agent 8080:80
```

ConfigMap 挂到 `/etc/aiops`，`CONFIG_DIR` 已在清单里指好。没建这个 ConfigMap 也能起（`optional: true`），
此时用镜像内置的 `/app/config` 默认值。改配置后 `kubectl rollout restart deploy/ai-ops-agent` 生效。

集群内运行时有两个便利点：

- **K8s 适配器免配置**：自动读取 ServiceAccount 挂载的 token 与 `ca.crt`，直连 `https://kubernetes.default.svc`，不用填 `KUBERNETES_API_URL`。
- **RBAC 只读**：`00-namespace-rbac.yaml` 里的 ClusterRole 仅含 `get / list / watch`，不会对集群做任何写操作。

> **必须单副本**：`replicas: 1` + `strategy: Recreate` 不是示例值，而是架构约束（进程内调度 + SQLite 单写 + PVC 是 `ReadWriteOnce`）。要 HA 需先把状态外置到 PostgreSQL，再给调度加选主。

### 3.4 用源码启动（本地开发）

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r deploy/requirements.txt

MOCK_MODE=true PORT=8080 python -m app.main
```

PowerShell 不支持前置赋值，改用：

```powershell
$env:MOCK_MODE="true"; $env:PORT="8080"; $env:LLM_API_KEY="sk-xxxxxxxx"
python -m app.main
```

前端两种模式：

```bash
cd frontend
npm install
npm run dev     # 开发用：http://127.0.0.1:5173，热更新，/api 已代理到 8080
npm run build   # 构建产物，之后由 FastAPI 在 / 直接托管
```

装成命令后用 `ops-cli` 代替 `python -m app.cli.ops_cli`：

```bash
pip install -e .
```

### 3.5 验证模型确实连通（3 秒，不用等诊断跑完）

服务起来后先做这一步，能省掉后面 90% 的「为什么报告像模板」的排查。

```bash
curl -X POST http://127.0.0.1:8080/api/v1/system/llm-test
```

成功：

```json
{"configured": true, "model": "deepseek-chat", "provider": "api.deepseek.com",
 "latency_ms": 812, "ok": true, "reply": "模型可用", "error": ""}
```

失败时会直接告诉你卡在哪一层，`error` 形如：

| 返回 | 含义 | 处理 |
| --- | --- | --- |
| `ok:false, error:"未配置模型密钥…"` | 环境变量没传进去 | 检查 `docker exec <c> env \| grep LLM` |
| `HTTP 401` | 密钥错 / 已失效 / 欠费 | 换 key |
| `HTTP 404` | `LLM_MODEL` 名字不对 | 按服务商文档改模型名 |
| `HTTP 400 ... model` | 该服务不支持这个模型名 | 同上 |
| `网络错误：ConnectTimeout` | 容器出不了网 / 需要代理 | 检查出口，或在 `LLM_BASE_URL` 用可达的代理地址 |

控制台等价操作：顶部绿色横幅右侧「测试连接」按钮，结果直接显示往返耗时；未配置时顶部是红色横幅。

### 3.6 启动后确认

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8080/` | 控制台（总览 / 告警 / 诊断 / 知识库 / 审计） |
| `http://127.0.0.1:8080/docs` | OpenAPI 交互文档，可直接试调接口 |
| `http://127.0.0.1:8080/healthz` | 健康检查：调度器状态、已跑轮次、运行时长、**模型是否接入** |

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok","version":"0.3.0","mock_mode":true,"scheduler_running":true,
#  "scheduler_rounds":8,"uptime_seconds":82.4,
#  "llm":{"configured":true,"model":"deepseek-chat","provider":"api.deepseek.com","mode":"llm"}}
```

`llm.mode` 为 `rules` 就说明当前没接模型——此时 `/api/v1/system/llm-test` 会给出具体原因。

`curl` 走本地代理导致 502 时，加 `--noproxy '*'` 或设 `NO_PROXY=127.0.0.1,localhost`。

---

## 4. 配置：文件挂载 + 环境变量覆盖

配置不再散落在代码里，也不再依赖仓库内的相对路径。**唯一事实来源是 `config/` 目录**，
运行时整目录挂载到容器即可，改配置不用重打镜像。

### 4.0 配置来源与优先级

```
构造参数  >  环境变量  >  .env  >  config/aiops.yaml  >  代码默认值
```

- **日常改配置**：编辑 `config/aiops.yaml`，重启容器生效。
- **临时覆盖某一项**：加环境变量，例如 `-e LLM_API_KEY=sk-xxx` 会盖掉文件里的 `llm.api_key`。
- 优先级高的赢，不存在「文件改了环境变量没生效」的歧义。

**配置文件发现顺序**（第一个存在的生效）：

| 顺序 | 位置 | 用途 |
| --- | --- | --- |
| 1 | `CONFIG_FILE` 指定的文件 | 完全自定义路径 |
| 2 | `CONFIG_DIR`/aiops.yaml | 挂载到非常规路径时用 |
| 3 | `/etc/aiops/aiops.yaml` | **容器挂载约定路径**（推荐） |
| 4 | `/app/config/aiops.yaml` | 镜像内置默认值 |

规则文件（检测规则、脱敏规则）与 `clusters.yaml` 的解析顺序相同：
**挂载目录优先 → 镜像内置兜底**。所以只挂 `aiops.yaml`、不带规则文件也能正常跑。

### 4.1 config/ 目录结构

```
config/
├── aiops.yaml              # 主配置：运行模式、数据源、模型、告警、鉴权、超时
├── rules-detection.yaml    # 检测规则：阈值 / 环比 / 3σ 基线 / 日志正则
├── rules-desensitize.yaml  # 脱敏规则：IP、Token、账号、主机名等
├── clusters.example.yaml   # 多集群模板，复制为 clusters.yaml 才生效
└── .env.example → 根目录   # 环境变量清单（仅覆盖时使用）
```

`config/` 里**没有任何密钥**，可以安全地进 Git；密钥通过环境变量或 Secret 注入。

### 4.2 三种挂载方式

**Docker（单容器）**

```bash
docker run -d --name ai-ops-agent -p 8080:8080 \
  -v $(pwd)/config:/etc/aiops:ro \
  -e LLM_API_KEY=sk-xxxx \
  -v $(pwd)/data:/data \
  ghcr.io/obc19/ai-ops-agent:latest
```

**Docker Compose**

`deploy/docker-compose.yml` 里已写好挂载，直接：

```bash
cd deploy && docker compose --env-file ../.env up -d --build
```

**Kubernetes**

ConfigMap 整目录挂载（仓库 `config/` 是唯一来源，不内联、不复制）：

```bash
kubectl -n aiops create configmap ai-ops-agent-config \
  --from-file=config/aiops.yaml \
  --from-file=config/rules-detection.yaml \
  --from-file=config/rules-desensitize.yaml
kubectl -n aiops rollout restart deploy/ai-ops-agent     # 改完配置重启生效
```

**不想挂载也能跑**：镜像内置了同一份 `config/`，不挂载时用内置默认值。
挂载的意义是「不改镜像就能改行为」。

### 4.3 修改后如何生效

| 内容 | 生效方式 |
| --- | --- |
| `aiops.yaml` 绝大多数项 | 重启进程 / 容器（`kubectl rollout restart`） |
| 检测规则、脱敏规则 | 同上；规则文件损坏时 **fail-closed**（跳过检测并告警），不会误判 |
| 环境变量 | 重启容器 |

> 配置写错不会让服务起不来：解析失败只打 ERROR 日志并回退到环境变量与默认值。
> 但**字段名写错会被明确告警**（`配置文件 ... 中存在无法识别的字段（已忽略）：xxx`），
> 避免「看起来配了、其实没生效」。

### 4.4 常见配置片段

```yaml
# 接真实数据源
runtime: {mock_mode: false}
sources:
  prometheus_url: http://prometheus.monitoring:9090
  elasticsearch_url: http://elasticsearch.logging:9200

# 换模型（任意 OpenAI 兼容服务）
llm:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model: qwen-plus

# 收紧检测
detection: {interval: 5, alert_cooldown: 600}
```

### 4.5 字段参考（对应 aiops.yaml 分区）


| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_MODE` | `true` | `true` 用内置 mock 数据源，无需外部依赖；`false` 走真实数据源 |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | 监听地址与端口，控制台与 API 共用 |
| `DATA_DIR` | `./data` | SQLite、向量库、审计日志的存放目录 |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DETECT_INTERVAL` | `10` | 轮询检测周期（秒），决定故障发现延迟 |
| `RETENTION_DAYS` | `30` | 告警 / 诊断 / 审计的保留天数（经验不自动清理） |

### 4.6 数据源（单集群）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PROMETHEUS_URL` | 空 | Prometheus 地址，如 `http://prometheus:9090`，留空跳过该适配器 |
| `ELASTICSEARCH_URL` | 空 | ES 地址，留空跳过 |
| `ELASTICSEARCH_INDEX` | `app-logs-*` | 日志索引通配 |
| `ELASTICSEARCH_USERNAME` / `ELASTICSEARCH_PASSWORD` | 空 | ES 基础认证（可选） |
| `JAEGER_URL` | 空 | Jaeger 查询地址 |
| `KUBERNETES_API_URL` / `KUBERNETES_TOKEN` | 空 | apiserver 地址与 Bearer Token；集群内运行可留空自动发现 |
| `KUBERNETES_CA_FILE` / `KUBERNETES_TOKEN_FILE` | 空 | 自定义 SA 挂载路径，留空用默认 `/var/run/secrets/kubernetes.io/serviceaccount/` |
| `TLS_VERIFY` | `true` | 数据源为自签证书时设 `false`，仅限可信内网 |
| `ENABLE_TRACE_AGENT` | `false` | 链路 Agent 开关 |
| `ENABLE_K8S_ADAPTER` | `false` | K8s 事件适配器开关 |

### 4.7 多集群

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CLUSTERS_CONFIG` | 空 | YAML 文件路径（如 `/etc/aiops/clusters.yaml`）或直接内联 YAML；留空视为单集群 |
| `DEFAULT_CLUSTER` / `DEFAULT_NAMESPACE` | `prod-cluster` / `prod` | 单集群模式下的归属名 |

示例见 `config/clusters.example.yaml`：

```yaml
clusters:
  - name: prod-cluster
    namespace: prod
    prometheus_url: http://prometheus.prod:9090
    elasticsearch_url: http://elasticsearch.prod:9200
    elasticsearch_index: app-logs-*
    jaeger_url: ""
    kubernetes_api_url: ""
  - name: staging
    namespace: staging
    prometheus_url: http://prometheus.staging:9090
```

未配置的数据源自动跳过；`GET /api/v1/clusters/sources` 可查看实际生效的集群与数据源。

### 4.8 模型与知识库

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek / `deepseek-chat` | 诊断使用的模型（OpenAI 兼容接口），敏感数据脱敏后同样经此外发 |
| `LLM_API_KEY` | 空 | 留空则不调用 LLM，Agent 全部走规则结论 |
| `LLM_TIMEOUT` | `20` | 单次 LLM 超时（秒） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | 空 | OpenAI 兼容 embedding；留空用本地哈希向量兜底 |
| `EMBEDDING_DIM` | `8` | 兜底哈希向量维度 |
| `KNOWLEDGE_BACKEND` | `simple` | `simple`（内嵌）/ `chroma` / `faiss` |
| `KNOWLEDGE_TOP_K` | `3` | RAG 检索返回条数 |
| `KNOWLEDGE_DEDUP_THRESHOLD` | `0.92` | 经验去重相似度阈值（同对象才判重） |
| `RAG_MIN_SCORE` | `0.15` | 经验命中最低分；接真实 embedding 后可调高 |

### 4.9 告警渠道

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ALERT_CHANNELS` | `feishu,dingtalk,email,webhook` | 启用哪些渠道，逗号分隔 |
| `FEISHU_WEBHOOK` | 空 | 飞书机器人 Webhook |
| `FEISHU_SECRET` | 空 | 飞书「加签」密钥（开启加签时必填） |
| `DINGTALK_WEBHOOK` | 空 | 钉钉机器人 Webhook |
| `DINGTALK_SECRET` | 空 | 钉钉「加签」密钥 |
| `ALERT_WEBHOOK_URL` | 空 | 通用 Webhook 地址 |
| `SMTP_HOST` / `SMTP_PORT` | 空 / `465` | 邮件服务器 |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_USE_TLS` | 空 / `true` | 邮件认证与 TLS |
| `ALERT_EMAIL_FROM` / `ALERT_EMAIL_TO` | 空 | 发件人 / 收件人（多个用逗号分隔） |
| `ALERT_GROUP_WINDOW` | `300` | 聚合窗口（秒）：同对象同指标在此窗口内合并 |
| `ALERT_COOLDOWN` | `300` | 通知冷却（秒）：避免同一告警反复推送 |

> mock 模式下所有通知打到内置接收端 `/_mock/sink`，方便本地验收，不会真的发到群里。

### 4.10 鉴权

| 变量 | 角色 | 权限 |
| --- | --- | --- |
| `AUTH_TOKEN` | admin | 全部权限，含注册集群、操作 mock 控制面 |
| `OPERATOR_TOKEN` | operator | 读 + 执行（触发诊断、确认/关闭告警、确认/回滚经验） |
| `VIEWER_TOKEN` | viewer | 只读 |

- 三个都不配 = **本地体验模式**，全部放行（审计里 actor 记为 `anonymous`）。
- 配了任意一个即开启鉴权：`GET` 需要 viewer，写操作需要 operator 及以上。
- 传递方式：`Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`。
- 控制台右上角有输入框，填 Token 后保存即可（存在浏览器本地）。

### 4.11 Agent 超时与检测规则

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_TIMEOUT` | `12` | 单 Agent 超时（秒），超时降级不阻塞整体 |
| `DIAGNOSIS_BUDGET` | `45` | 单次诊断总预算（秒），保证端到端 ≤60s |
| `MOCK_SINK_BASE` | 空 | mock 通知接收端地址，默认跟随 `PORT` |

检测规则本身在 `app/detection/rules.yaml`（阈值、环比、3σ、日志关键词/正则），可直接改文件后重启生效。

---

## 5. 基本用法

### 5.1 控制台

| 页面 | 能看到什么 |
| --- | --- |
| 总览 | 告警/诊断统计、集群与数据源健康度、运行模式（Mock 会明确标注）、mock 注入的故障 |
| 告警 | 按级别（P0/P1/P2）与状态过滤；点开看详情与 Markdown 报告；确认 / 关闭 / 复发重开 |
| 诊断 | 诊断列表与完整报告（Markdown 预览可切源码），含各 Agent 依据与 RAG 命中经验 |
| 知识库 | 经验草稿 / 已确认 / 已回滚，支持语义检索与回滚 |
| 审计 | 全量操作留痕；`egress_only` 可只看数据外发记录 |

### 5.2 API

Base 路径 `/api/v1`，交互式文档 `/docs`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/overview` | 全局总览（统计、数据源健康、运行模式） |
| POST / GET | `/clusters` | 注册集群 / 集群列表 |
| GET | `/clusters/sources` | 已配置集群与其数据源（凭据脱敏） |
| GET | `/clusters/{name}/health` | 集群数据源健康度 |
| POST | `/diagnostics` | 手工构造异常并触发诊断 |
| POST | `/diagnostics/detect` | 立即跑一轮采集 + 检测 + 诊断 |
| GET | `/diagnostics` / `/diagnostics/{id}` | 诊断列表 / 详情 |
| GET | `/diagnostics/{id}/markdown` | 导出 Markdown 报告 |
| GET | `/alerts` | 告警列表（可按 severity / status / cluster 过滤） |
| GET | `/alerts/{id}` / `/alerts/{id}/markdown` | 告警详情 / Markdown |
| POST | `/alerts/{id}/confirm` · `/resolve` · `/reopen` · `/renotify` | 确认 · 关闭 · 复发重开 · 重发 |
| GET | `/knowledge` / `/knowledge/stats` / `/knowledge/search` | 经验列表 / 统计 / 语义检索 |
| GET | `/knowledge/{id}` | 经验详情 |
| POST | `/knowledge` | 由诊断报告生成经验草稿 |
| POST | `/knowledge/{id}/confirm` · `/rollback` | 确认入库 · 回滚 |
| POST | `/ingest/alertmanager` | 接收 Alertmanager Webhook，直接进入诊断闭环 |
| GET | `/audit` | 审计日志（`egress_only=true` 只看外发） |

Mock 控制面（仅 mock 模式有意义）：`POST /_mock/fault`、`POST /_mock/reset`、`GET /_mock/faults`、`GET /_mock/sink`、`DELETE /_mock/sink`、`POST /_mock/echo`。

### 5.3 命令行

`ops-cli` 是对 REST API 的封装，默认连 `http://127.0.0.1:8080`。

```bash
ops-cli --api http://127.0.0.1:8080 --token <TOKEN> <子命令>

ops-cli health                       # 健康检查
ops-cli overview                     # 全局总览
ops-cli detect                       # 立即跑一轮采集与检测
ops-cli diagnose --target order-service --metric cpu_usage_percent --severity P1
ops-cli alerts --severity P0 --status notified
ops-cli alert-confirm <告警ID> --by alice --note "已扩容"
ops-cli alert-resolve <告警ID> --by alice
ops-cli alert-reopen  <告警ID> --by alice
ops-cli clusters                     # 已配置集群与数据源
ops-cli ingest --file alert.json     # 把 Alertmanager 告警灌入闭环
ops-cli report <诊断ID>              # 打印诊断报告 Markdown
ops-cli knowledge list --status draft
ops-cli knowledge stats
ops-cli knowledge search --query "CPU 飙升"
ops-cli knowledge confirm <经验ID> --by alice
ops-cli knowledge rollback <经验ID> --reason "重复经验"
ops-cli audit --limit 50 --egress
```

> 告警「重发」（`POST /alerts/{id}/renotify`）目前只有 API，CLI 未提供对应子命令。

---

## 6. 常见使用示例

### 6.1 五分钟跑通完整闭环（mock 模式）

```bash
# 1) 启动（mock 模式）
docker run -d --name ai-ops-agent -p 8080:8080 -e MOCK_MODE=true \
  -v $(pwd)/data:/data ghcr.io/obc19/ai-ops-agent:latest

# 2) 注入一个 CPU 故障：order-service
curl -s -X POST http://127.0.0.1:8080/_mock/fault \
  -H "Content-Type: application/json" \
  -d '{"target":"order-service","metric":"cpu_usage_percent","severity":"P0","magnitude":3.0}'

# 3) 等一个检测周期（默认 10s），或者立刻强制检测
curl -s -X POST http://127.0.0.1:8080/api/v1/diagnostics/detect \
  -H "Content-Type: application/json" -d '{"notify":true}'

# 4) 看告警与诊断
curl -s "http://127.0.0.1:8080/api/v1/alerts?severity=P0"
curl -s "http://127.0.0.1:8080/api/v1/diagnostics" | head -c 400

# 5) mock 模式下四个渠道的通知都进了接收端
curl -s http://127.0.0.1:8080/_mock/sink | head -c 400

# 6) 清场
curl -s -X POST http://127.0.0.1:8080/_mock/reset
```

### 6.2 接入真实 Prometheus + Elasticsearch

```bash
export MOCK_MODE=false
export PROMETHEUS_URL=http://prometheus.monitoring.svc:9090
export ELASTICSEARCH_URL=http://elasticsearch.logging.svc:9200
export ELASTICSEARCH_INDEX='app-logs-*'
export LLM_API_KEY=sk-xxxxxxxx
python -m app.main
```

验证：

```bash
curl -s http://127.0.0.1:8080/api/v1/clusters/sources   # 看数据源是否配好
curl -s http://127.0.0.1:8080/api/v1/clusters/prod-cluster/health
```

> 指标查询用 PromQL 聚合到 `namespace, pod` 维度，因此 Prometheus 中指标需带这两个标签；否则按目标名匹配会失败。

### 6.3 接收 Alertmanager 告警

Alertmanager 配置：

```yaml
receivers:
  - name: aiops
    webhook_configs:
      - url: http://ai-ops-agent.aiops.svc:8080/api/v1/ingest/alertmanager
        send_resolved: false
```

开启鉴权时，Webhook 无法带自定义头可改用 `X-Auth-Token` 查询参数或关闭该路径鉴权（二选一，推荐在内网部署并把 NetworkPolicy 收紧）。

灌进来的告警会被映射为异常事件（`critical→P0`、`warning→P1`、`info→P2`），复用同一套诊断、告警与知识库流程。

命令行也能手工灌入：

```bash
ops-cli ingest --file alert.json
ops-cli ingest --target payment-service --severity P1 --summary "5xx 激增"
```

### 6.4 飞书 / 钉钉机器人加签

机器人开启「加签」后，把密钥配上即可，签名由后端计算：

```bash
export FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxx'
export FEISHU_SECRET='<飞书加签密钥>'

export DINGTALK_WEBHOOK='https://oapi.dingtalk.com/robot/send?access_token=xxx'
export DINGTALK_SECRET='<钉钉加签密钥>'
```

飞书的签名放在请求体 `timestamp` + `sign` 字段，钉钉放在 URL query，均已实现。只想开某一个渠道：

```bash
export ALERT_CHANNELS=feishu
```

### 6.5 经验沉淀与 RAG 命中

```bash
# 1) 用一条诊断报告生成经验草稿
curl -s -X POST http://127.0.0.1:8080/api/v1/knowledge \
  -H "Content-Type: application/json" -d '{"diagnosis_id":"<诊断ID>"}'

# 2) 人工确认入库（confirmed_by 必填）
curl -s -X POST http://127.0.0.1:8080/api/v1/knowledge/<经验ID>/confirm \
  -H "Content-Type: application/json" -d '{"confirmed_by":"alice"}'

# 3) 再次触发同类故障，报告里会出现 rag_hits
curl -s -X POST http://127.0.0.1:8080/_mock/fault \
  -H "Content-Type: application/json" -d '{"target":"order-service"}'
```

确认入库前经验文本会先脱敏；同一对象（同集群 + 同 target）的相似经验会被判重，跨对象则各自留存。误入库可回滚：

```bash
ops-cli knowledge rollback <经验ID> --reason "重复经验"
```

### 6.6 多集群接入

```bash
cp config/clusters.example.yaml config/clusters.yaml
# 编辑 clusters.yaml 填各集群地址，然后重启服务
```

`config/clusters.yaml` 存在即自动加载，**不用配 `CLUSTERS_CONFIG`**。
放在别处时再用 `CLUSTERS_CONFIG=/path/to/clusters.yaml` 指定。

事件与告警都会带集群维度，控制台与 API 可按集群过滤；`GET /api/v1/clusters/sources` 列出所有生效的集群和数据源。

### 6.7 开启鉴权后的调用

```bash
export AUTH_TOKEN=admin-token-xxx
export OPERATOR_TOKEN=op-token-xxx
export VIEWER_TOKEN=view-token-xxx

# 只读接口用 viewer 即可
curl -s -H "Authorization: Bearer view-token-xxx" http://127.0.0.1:8080/api/v1/alerts

# 写操作用 operator
curl -s -X POST -H "X-Auth-Token: op-token-xxx" \
  -H "Content-Type: application/json" \
  -d '{"confirmed_by":"alice"}' \
  http://127.0.0.1:8080/api/v1/alerts/<告警ID>/confirm
```

### 6.8 敏感数据的外发行为

本项目不再内置本地模型通道，全部诊断请求都走外部 API。脱敏网关是唯一出口，行为固定为：

| 情形 | 行为 |
| --- | --- |
| 内容未命中敏感规则 | 原文外发 |
| 内容命中敏感规则 | **先脱敏再外发**，审计记录 `channel=external-redacted` 与命中的规则明细 |
| 脱敏引擎异常 | 拦截（fail-closed），不外发，写审计 + 触发 P1 自监控告警 |
| 脱敏后二次扫描仍命中 | 拦截（fail-closed），同上 |

验证方式（每一步都可查）：

```bash
# 1) 看外发通道与命中的敏感规则
curl -s "http://127.0.0.1:8080/api/v1/audit?egress_only=true&limit=10" | python3 -m json.tool
# 2) 看是否发生过拦截
curl -s "http://127.0.0.1:8080/api/v1/audit?action=llm_egress_blocked&limit=10"
```

> 需要「敏感数据完全不出网」的组织，应改为在**内网自建一个 OpenAI 兼容端点**（如 vLLM + Qwen），
> 把 `LLM_BASE_URL` 指向它即可，代码无需改动——这与"本地模型"是同一件事，只是不再作为独立配置项存在。

---

## 7. 数据、备份与运维

| 文件（位于 `DATA_DIR`） | 内容 |
| --- | --- |
| `aiops.db` | SQLite：告警、诊断、经验、集群、审计索引 |
| `vectors.json` | 内嵌向量库（`KNOWLEDGE_BACKEND=simple` 时） |
| `audit.jsonl` | 审计流水，逐行 JSON |

- **备份**：停容器后打包 `DATA_DIR` 即可（`tar czf aiops-data.tgz data/`），恢复时原路放回。
- **清理**：告警/诊断/审计按 `RETENTION_DAYS` 自动过期，经验不会自动删；要清空全部数据直接删 `DATA_DIR`。
- **升级**：`docker compose pull && docker compose up -d`，SQLite 表结构向后兼容；升级前建议先备份 `DATA_DIR`。
- **健康检查**：容器内置 `HEALTHCHECK` 打 `/healthz`；K8s 清单已配 readiness/liveness 探针。
- **容量参考**：单实例 ≤1000 Pod 的集群开箱可用；1000~3000 建议调大 `DETECT_INTERVAL` 或在 PromQL 侧收敛 namespace。

---

## 8. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 打开首页只看到「后端已启动，前端控制台尚未构建」 | 源码方式运行且没构建前端。执行 `cd frontend && npm install && npm run build` 后刷新 |
| Windows 控制台日志中文乱码 | 控制台代码页为 936。启动前 `set PYTHONIOENCODING=utf-8`（PowerShell：`$env:PYTHONIOENCODING="utf-8"`） |
| 端口被占用 | 换端口 `--server.port` 不适用本项目，直接 `PORT=8090 python -m app.main`；注意前端 dev 代理写死 8080，换端口需同步改 `frontend/vite.config.ts` |
| `curl` 访问 127.0.0.1 报 502 / 连不上 | 系统代理拦截了回环地址。加 `--noproxy '*'`，或设 `NO_PROXY=127.0.0.1,localhost`。应用内部对数据源与渠道的 HTTP 调用已统一 `trust_env=False`，不受影响 |
| 告警「发了但收不到」 | mock 模式下通知只进 `/_mock/sink`；真实模式检查 `ALERT_CHANNELS` 是否包含该渠道、Webhook 是否可达、加签密钥是否正确 |
| 告警反复重发 | 调大 `ALERT_COOLDOWN`；P0 不参与抑制，会按升级策略重新通知，属预期行为 |
| 数据源显示不可达 | `GET /api/v1/clusters/{name}/health` 看具体错误；自签证书先设 `TLS_VERIFY=false` 验证是否为证书问题 |
| K8s 内访问 apiserver 403 | RBAC 权限不足。用的是只读 ClusterRole，若改为自定义角色，确认包含 `get/list/watch` |
| 诊断报告置信度低 / 没有 RAG 命中 | 未配 `LLM_API_KEY` 时走规则结论，置信度天然偏低；经验库为空或 `RAG_MIN_SCORE` 过高也会无命中，可下调阈值或先沉淀几条经验 |
| 页面强刷后样式没变 | 静态资源文件名带 hash，普通刷新即可；个别浏览器缓存严格时用 Ctrl+F5 |

---

## 9. 自测与验收

```bash
python tools/demo.py            # 或 make demo，端到端自证 8 条验收标准
python -m pytest -q             # 单元与接口测试
```

`demo.py` 会自动拉起 mock LLM 与本地服务，逐条验证：

| # | 验收项 | 判定方式 |
| --- | --- | --- |
| 1 | 三类数据源可接入可查询且可外接 | `/overview` 中适配器 `reachable=true` |
| 2 | 模拟故障 60s 内完成诊断并分级告警 | 注入 CPU 故障 → 计时 `/diagnostics/detect` |
| 3 | `docker compose up` 一键启动 | 有 Docker 时实际构建启动，否则 SKIP |
| 4 | 镜像 ≤300MB、启动 ≤5s | `docker image inspect` + `/healthz` 计时，否则 SKIP |
| 5 | 脱敏后外发不含原始敏感值 | mock LLM 捕获请求体，断言无 IP / Token / 邮箱 |
| 6 | 经验确认入库并被 RAG 命中 | 确认 → 再触发相似故障 → 断言 `rag_hits` 非空 |
| 7 | 至少一种渠道成功送达 | 内置接收端 `/_mock/sink` 断言四渠道 payload |
| 8 | 开源要素齐备 | LICENSE / README / CONTRIBUTING / SECURITY / CI 存在性 |

无 Docker 的环境下第 3、4 项自动跳过，其余应全通过。

---

## 11. 附：仓库结构对照（v0.4 重构）

为了让「程序」和「配置 / 文档 / 脚本」彻底分开，仓库做了一次结构收敛。老脚本如果引用了旧路径，按这张表改：

| 旧路径 | 新路径 | 说明 |
| --- | --- | --- |
| `requirements.txt` | `deploy/requirements.txt` | 依赖清单归到构建目录，镜像里用完即删 |
| `scripts/demo.py` | `tools/demo.py` | 开发脚本与运行时 `app/` 分离，不打进镜像 |
| `USAGE.md` | `docs/USAGE.md` | 文档统一进 `docs/` |
| `CHANGELOG.md` | `docs/CHANGELOG.md` | 同上 |
| `CONTRIBUTING.md` | `docs/CONTRIBUTING.md` | 同上 |
| `SECURITY.md` | `docs/SECURITY.md` | 同上 |
| `app/detection/rules.yaml` | `config/rules-detection.yaml` | **规则文件从代码包移出**，改为配置目录读取 |
| `app/gateway/rules.yaml` | `config/rules-desensitize.yaml` | 同上 |
| `deploy/clusters.example.yaml` | `config/clusters.example.yaml` | 与其它配置放一起 |

保留在根目录的：`README.md`（门面）、`LICENSE`、`Makefile`、`pyproject.toml`（打包元数据，PEP 621 要求在根）、`.env.example`（环境变量清单）。

**没有移出仓库的东西**：`requirements.txt` 与 `pyproject.toml` 是构建与安装的输入，移出仓库会让 `docker build`、`pip install -e .` 直接失效，所以只做「归位 + 不进运行镜像」，不做「移出仓库」。同理 `frontend/package.json` 必须留在前端目录（npm 约定）。

**关键变化**：此前有两处配置是「写死路径指向仓库内文件」——检测规则和脱敏规则分别是 `app/detection/rules.yaml`、`app/gateway/rules.yaml`。现在它们由 `Settings.detection_rules_path` / `desensitize_rules_path` 从配置目录解析，
顺序是「挂载目录 → 镜像内置」，所以改规则只需要改挂载目录里的 YAML，重启即可，不用重建镜像。


---

## 10. 能力边界

- **诊断不自动处置**：只给结论与建议，重启、扩缩容等写操作需人工执行。
- **单副本**：进程内调度 + SQLite 单写，多副本会重复检测与重复告警。
- **Agent 为单轮推理**：规则 + LLM 双路径，尚未做多轮工具调用与自主规划。
- **链路 / K8s 适配器默认关闭**：需显式打开 `ENABLE_TRACE_AGENT` / `ENABLE_K8S_ADAPTER`。
- **内嵌向量库适合中小规模**：经验上万条建议切 `KNOWLEDGE_BACKEND=chroma`。
- **不产出 PDF**：报告为内部 JSON + 对外 Markdown（`/diagnostics/{id}/markdown`）。

路线图（自动处置、告警 AI 降噪、多租户 RBAC、Helm 与多副本、插件市场）见 [README](../README.md#路线图)。

---

其他文档：[README](../README.md)　｜　[贡献指南](CONTRIBUTING.md)　｜　[安全说明](SECURITY.md)　｜　[变更记录](CHANGELOG.md)
