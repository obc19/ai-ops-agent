# 变更日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
