# 安全说明

## 报告漏洞

请**不要**通过公开 issue 报告安全问题。发送邮件至仓库维护者（见 GitHub 个人主页），或使用 GitHub 的
[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities)。

请在报告中包含：影响版本、复现步骤、影响面评估、可选的修复建议。我们会在 7 天内响应。

## 安全设计

本工具会接触生产环境的日志、指标与集群事件，因此默认采取以下约束：

| 机制 | 说明 |
| --- | --- |
| 最小外发 | 只有根因归纳与经验向量化会调用外部服务，且必经脱敏网关 |
| 脱敏二次校验 | 脱敏后再扫一次，仍有命中则拦截外发（fail-closed） |
| 审计留痕 | 每次外发记录 `action=llm_egress`，含脱敏后预览、规则命中、调用方；`/api/v1/audit?egress_only=true` 可查 |
| 本地模型隔离 | 配置 `LOCAL_MODEL_ENABLED=true` 后，命中敏感规则的请求改走本地模型，不出境 |
| 只读采集 | 四个适配器仅做只读 API 查询，不写入外部系统 |
| 非 root 运行 | 容器内以 `appuser` 运行，数据目录独立挂载 |
| 鉴权 | 配置 `AUTH_TOKEN` 后所有业务 API 需 Bearer Token；**默认未开启，请勿直接暴露公网** |

## 部署建议

- 不要把控制台直接暴露在公网；如需外网访问，请置于反向代理 + HTTPS + 身份认证之后
- `LLM_API_KEY`、`SMTP_PASSWORD` 等通过密钥管理注入，不要写进镜像或提交到仓库
- 日志与审计目录（`DATA_DIR`）应限制访问权限，其中包含告警与诊断上下文
- 定期执行 `docker scout` / `trivy` 扫描镜像，关注依赖漏洞公告

## 已知限制

- Webhook 渠道未实现飞书/钉钉的签名校验，接入正式机器人时建议在网关层加签
- 内置向量库为文件存储，未做加密；如需存放高敏经验请挂载加密卷或切换外部向量库
- 默认的单 Token 鉴权适合内网；多租户场景请等待 RBAC 能力
