# 变更日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.6.0] - 2026-10-30

补上「从有数据到能定位」之间缺的那一层：**服务依赖拓扑与根因收敛**。

此前 5 个服务同时异常就产生 5 条独立告警、5 份独立报告，每份都在猜自己的根因，
而人还是得自己去判断「其实是 payment 挂了把 order 拖垮」。
AIOps 最核心的价值恰恰是把 N 条收敛成 1 条，这次补的就是它。

### 新增

- **服务依赖拓扑**（`app/topology/`）
  - 从 Jaeger span 的父子关系（`processID` → `serviceName`、`references` → 父 `spanID`）自动推导
    `caller → callee` 边，跨服务的才算依赖，同服务内部调用剔除
  - trace 采样不足时可用 `topology.dependencies` 手工声明兜底，两路合并而非替代
  - 提供上游/下游/可达下游/拓扑序/环检测，结果缓存 `topology.refresh_seconds` 秒
  - 新增 `GET /api/v1/topology`，返回节点、边、入口、末端、文本拓扑与统计
- **根因收敛**（`app/topology/correlator.py`）
  - 沿拓扑序从上游向下游传播归属：上游已归属则并入该群，否则自建为新群（自身即源头）
  - 同一对象的多个指标异常先合并为单个「异常节点」，取最严重的一条作为代表
  - `topology.escalate_by_impact=true` 时，根因被 N 个下游连带会提升到群内最严重等级，
    并在摘要里注明提升原因
  - **没有拓扑数据时不猜**：每条异常独立成案并在理由里写明「未做关联」——
    宁可少收敛，也不能把两起真实故障硬并成一个
- **诊断链路改为「先收敛再诊断」**（`services/pipeline.py`）
  - 一组异常只做一次完整诊断，根因节点生成告警，同源异常作为「受影响」附入报告，
    不再各自发告警
  - 报告新增 `correlation`（根因/成员/理由/影响面/抑制条数）与 `topology`（上下游/文本拓扑）
  - Markdown 报告新增「同源影响」「服务拓扑」两节
- **拓扑感知的根因推断**：`DiagnosisContext` 带上上下游，规则模式在依据里说明
  「下游链路为 X，它们若同时异常应优先怀疑本对象」；LLM prompt 追加拓扑块与
  「上下游同时异常时根因落在更上游」的判断要求
- **控制台**：概览页新增「服务依赖拓扑」卡片；诊断列表与详情显示「收敛 +N」标记

### 修复

- **日志触发时指标维度直接降级**：`metric` 为 `log-timeout` 这类规则 id 时，
  指标 Agent 找不到同名指标就整个跳过，导致报告「部分降级」、置信度被无谓拉低（0.44）。
  改为退而分析该对象偏离最大的指标，并在依据里说明换看原因；全部指标都无明显偏离时仍如实跳过
- **日志规则无根因映射**：此前 `log-timeout` 等规则命中后只会输出「需结合日志与链路进一步确认」，
  现在为 6 类日志规则配了具体根因与可执行处置步骤

### 变更

- 诊断报告新增两个字段（`correlation` / `topology`），旧报告读取时按默认空值兼容

## [0.5.0] - 2026-10-30

补齐「监测服务器状态」这一层。此前平台只有容器与应用指标，
管理员真正排查时第一眼看的**服务器本体**（负载、IO 等待、fd、连接数、僵尸进程）完全没有信号。

### 新增

- **服务器本体采集**（`app/collectors/host_adapter.py`）：直读 `/proc`，零外部依赖、不执行任何命令
  - 指标：每核负载、CPU 使用率、IO 等待、内存、Swap、磁盘、文件描述符、已建立连接、进程数、僵尸进程
  - `SERVER_PROC_ROOT` 可配置：容器把宿主机 `/proc` 挂到 `/host/proc` 即可监测宿主机
  - 维护内存滑动序列，使环比与 3σ 基线同样适用于主机层
- **主动拨测**（`app/collectors/probe_adapter.py` + `config/probes.yaml`）：平台自己访问目标，
  把一次访问拆成 `DNS / 建连 / TLS / TTFB / 总耗时` 五段，直接回答「用户访问慢在哪一段」
  - 首次请求做预热后丢弃，避免冷启动开销虚高导致误报
  - 请求失败时只报「不可用」一条，不再同时冒出 total / ttfb 三条重复告警
- **服务器状态接口** `GET /api/v1/host`：返回主机指标与拨测结果，控制台首屏新增两块卡片
- **服务器层检测规则**：11 条阈值 + 2 条环比，阈值在 `config/rules-detection.yaml` 里改
- **根因与修复方案**：主机层与拨测层各指标都配了可执行步骤（`top` / `iostat` / `ss` / `lsof` 等具体命令）
- **`exclude_metrics`**：基线检测可排除天然波动或单调增长的指标，避免磁盘占用缓慢上升天天误报

### 修复

- **根因建议硬编码服务名**：CPU 异常的处置步骤里写死了 `order-service`，对任何其他目标都会给错建议；
  改为按实际对象渲染
- **拨测冷启动误报**：建连耗时被一次性初始化开销抬高约 300ms，导致每次启动都误报 P2

### 部署

- `docker-compose.yml` 增加 `-v /proc:/host/proc:ro`、`-v /:/host/root:ro`
- K8s 清单增加 `hostPath: /proc` 只读挂载与 `SERVER_PROC_ROOT` 环境变量

## [0.4.0] - 2026-10-27

配置与代码、文档与交付物彻底分离：运行时行为由一份可挂载的 YAML 决定，
不再依赖「仓库内某个相对路径的文件」。

### 新增

- **YAML 配置源**：`config/aiops.yaml` 成为唯一配置源，优先级 `环境变量 > .env > aiops.yaml > 代码默认值`
- **目录挂载**：`-v $(pwd)/config:/etc/aiops:ro` 即可整体替换配置，无需重建镜像；规则文件同样从挂载目录读取
- **配置发现顺序**：`CONFIG_FILE` → `CONFIG_DIR/aiops.yaml` → `/etc/aiops/aiops.yaml` → 镜像内置 `/app/config/aiops.yaml`
- **字段校验提示**：配置文件里出现无法识别的字段时明确告警，不再静默忽略
- **多集群免配置**：`config/clusters.yaml` 存在即自动加载，不必再设 `CLUSTERS_CONFIG`

### 变更

- **规则文件移出代码包**：`app/detection/rules.yaml` → `config/rules-detection.yaml`；
  `app/gateway/rules.yaml` → `config/rules-desensitize.yaml`
- **仓库结构收敛**：文档进 `docs/`、依赖清单进 `deploy/`、开发脚本进 `tools/`；
  运行镜像只 COPY `app/` + `config/` + 前端产物
- **K8s 配置**：改用 `kubectl create configmap --from-file=config/` 整目录挂载，清单不再内联配置

### 兼容性

- 所有原有环境变量继续有效（优先级最高），老部署方式不需要改
- 配置文件缺失或解析失败时回退环境变量与默认值，不会阻断启动
- `requirements.txt`、`pyproject.toml` 保留在仓库内：它们是构建与安装的输入，移出会导致 `docker build` / `pip install -e .` 失效

## [0.3.0] - 2026-10-26

把「模型到底接没接上」从隐性前提变成显性状态。此前未配置密钥时系统静默降级为规则模式，
用户在控制台看不出任何差别，误以为看到的模板结论是 AI 分析结果。

### 新增

- **模型连通性探针**：`POST /api/v1/system/llm-test` 真实发起一次最小请求，返回 `ok` / `latency_ms` / `reply` / `error`；401、404、网络超时分别可辨
- **模型状态接口**：`GET /api/v1/system/llm` 报告 `configured` / `model` / `provider` / `api_key_masked`（仅前 4 位与后 4 位），完整密钥永不回显
- **健康检查扩展**：`/healthz` 增加 `llm: {configured, model, provider, mode}`，`mode` 为 `llm` 或 `rules`
- **控制台状态横幅**：未配置时顶部红条写明「当前没有接入大模型」，附可复制的配置片段；已配置时绿条显示模型与厂商，并带「测试连接」按钮
- **启动日志告警**：未配置密钥时打印带边框的 WARNING，说明当前是规则模式及配置方法

### 变更

- **移除本地模型通道**：`LOCAL_MODEL_URL` / `LOCAL_MODEL_NAME` / `LOCAL_MODEL_ENABLED` 及相关分流逻辑全部删除，诊断请求统一走外部 OpenAI 兼容 API（自建模型可把 `LLM_BASE_URL` 指向 vLLM / Ollama 的 `/v1`）
- `.env.example` 把模型配置提到第一优先级，并补充各服务商 `base_url` / `model` 对照说明
- README 快速开始增加第 0 步「先配模型」，并说明规则模式与 AI 模式的差别

### 已知限制

- 密钥仍需通过环境变量提供，镜像不内置任何凭据
- 模型返回非 JSON 时根因 Agent 回退规则结论，报告标注「LLM 不可用或返回不合规」

## [0.2.0] - 2026-10-26

在 v0.1 骨架之上补齐运营闭环所需的功能，未改动项目结构。

### 新增

- **告警状态机**：`triggered → notified → confirmed → resolved`，非法流转返回 409，全程写入 `status_history`；新增 `POST /alerts/{id}/reopen` 支持复发重开，通知成功后自动推进为「已通知」
- **三角色静态鉴权**：`VIEWER_TOKEN` / `OPERATOR_TOKEN` / `AUTH_TOKEN` 对应 viewer / operator / admin；读接口需 viewer、写接口需 operator，三者都不配置时保持本地体验模式全放行
- **多集群接入**：`CLUSTERS_CONFIG`（YAML 文件或内联）描述多个集群，适配器按集群实例化，事件与告警天然带 `cluster`/`namespace`；新增 `GET /clusters/sources`，`ops-cli clusters`
- **Alertmanager 接入**：`POST /ingest/alertmanager` 直接消费 Alertmanager v4 Webhook，映射为统一异常事件后复用诊断、告警与知识库；`ops-cli ingest` 支持命令行或 JSON 文件灌入
- **脱敏二次校验与自告警**：脱敏后再扫描残留敏感串，命中即拦截；脱敏引擎异常或残留命中都会写审计并生成工具自身 P1 告警
- **飞书 / 钉钉加签**：`FEISHU_SECRET` / `DINGTALK_SECRET` 配置后自动按官方算法签名，mock 接收端不加签
- **总览增强**：`/overview` 增加集群列表、数据源归属与鉴权开关状态

### 变更

- 采集适配器统一以「集群」为上下文，修正 Prometheus/ES 真实查询里 `cluster` 误写成应用名的问题
- CLI 新增 `clusters`、`ingest`、`alert-reopen`，`alerts --status` 支持 `notified`
- 控制台告警状态显示中文标签，新增「已通知」筛选项；令牌输入提示区分角色

### 已知限制

- 链路与 K8s 模块默认关闭，需显式开启
- 未提供 Helm Chart、多租户与细粒度 RBAC（当前为静态三角色）
- Docker 相关验收项需本机安装 Docker 后重跑

## [0.1.0] - 2026-10-26

首个骨架版本：全模块端到端可跑通（mock 数据源），Agent 支持规则引擎与 LLM 双路径。

### 新增

- **采集层**：`BaseCollector` 抽象 + Prometheus / Elasticsearch / Jaeger / Kubernetes 四个只读适配器，未配置自动跳过，单源失败自动重试并降级
- **Mock 世界**：可注入故障的仿真数据源（指标/日志/链路/K8s 事件），支撑无外部依赖的端到端演示与验收
- **检测引擎**：静态阈值、百分比环比、3σ 滑动基线、日志关键词/正则规则；规则外置 `rules.yaml`，同对象同指标去重保留最高级别
- **智能体**：指标 / 日志 / 链路 / 根因四个 Agent，输出「置信度 + 依据」；编排中枢支持单 Agent 超时（默认 12s）、总预算（45s）、失败降级与可解释回溯；链路 Agent 默认关闭可插拔
- **数据安全网关**：正则脱敏（IP / Token / 账号 / 邮箱 / 手机号 / 主机名），打码与替换两种策略，外发二次校验 fail-closed，敏感数据可路由到本地模型，全部外发留审计
- **告警**：P0/P1/P2 分级、同对象同指标聚合、P0 升级与冷却、飞书/钉钉/邮件/Webhook 四渠道、状态机（触发→确认→关闭）
- **知识库**：草稿 → 人工确认 → 入库 → 语义检索 → 回滚；入库前脱敏、同对象相似度去重、跨对象相似故障各自留存
- **存储**：SQLite 保存告警/诊断/异常/经验/审计，按 `RETENTION_DAYS` 自动清理（经验不删）
- **接口**：`/api/v1` 全量 REST + `/healthz`，静态 Token 鉴权
- **CLI**：`ops-cli` 覆盖诊断触发、告警处理、知识库管理、审计查询、报告导出
- **控制台**：Vue 3 + TS + Vite 只读总览（总览/告警/诊断/知识库/审计），产物打包进同一镜像
- **部署**：多阶段 Dockerfile（前端构建 + Python 运行时）、docker-compose、单容器 `docker run`、健康检查、非 root 运行
- **工程**：pytest 25 项测试、`scripts/demo.py` 一键验收自证、GitHub Actions CI/Release

### 已知限制

- 链路与 K8s 模块默认关闭，需显式开启
- 飞书/钉钉 Webhook 未实现签名校验
- 未提供 Helm Chart 与多租户 RBAC
