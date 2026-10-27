# 贡献指南

感谢参与 ai-ops-agent！这份工具的目标是「轻量、可审计、开箱即用」，所有贡献都应围绕这三条展开。

## 开发环境

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r deploy/requirements.txt

# 前端（可选）
cd frontend && npm install && cd ..

# 跑测试
python -m pytest -q

# 跑端到端自证
python tools/demo.py
```

约定：

- Python 3.11+，行宽 100，类型标注尽量补全
- 不引入重型依赖（>50MB 或需要常驻进程的组件需先讨论）
- 新增外部依赖必须同步更新 `deploy/requirements.txt` 与 `pyproject.toml`

## 提交前检查

1. `python -m pytest -q` 全绿
2. `python tools/demo.py` 无新增 FAIL
3. 提交信息格式：`<type>: <一句话说明>`，type 取 `feat|fix|test|docs|chore|refactor`
4. 一个提交只做一件事，禁止把格式化与功能混在一起

## 扩展点

| 想加什么 | 怎么做 |
| --- | --- |
| 新数据源 | 继承 `app/collectors/base.py::BaseCollector`，实现 `configured` 与 `query`，在 `app/collectors/__init__.py` 注册 |
| 新检测规则 | 改 `config/rules-detection.yaml`，无需改代码；复杂逻辑在 `app/detection/` 加模块并在 `engine.py` 串起来 |
| 新 Agent | 继承 `app/agents/base.py::BaseAgent`，返回带 `confidence` 与 `evidence` 的片段，在 `Orchestrator.agents` 注册 |
| 新告警渠道 | 继承 `app/alerting/channels/base.py::BaseChannel`，实现 `build_payload` 与 `configured_url`，在 `CHANNEL_TYPES` 注册 |
| 新脱敏规则 | 改 `config/rules-desensitize.yaml`（正则 + action），注意必须通过「二次校验」 |
| 新配置项 | 在 `app/config/settings.py` 加字段 → 写进 `config/aiops.yaml`（带注释）→ 补 `tests/test_config_source.py` 用例 |
| 换向量库 | 实现 `VectorStore` 协议，在 `app/knowledge/vector_store.py::build_vector_store` 分支 |

## 安全相关

- 涉及数据外发的改动，必须在 `app/gateway` 里走统一出口，禁止在 Agent 内直接调用外部 API
- 脱敏失败必须 fail-closed，不要添加「失败则放行」的分支
- 新增日志字段时确认不含原始 IP / Token / 账号

## 提交 PR

1. Fork 并新建分支：`feat/xxx`
2. 写清变更动机、影响面、验证方式（贴 `demo.py` 输出更好）
3. CI 必须通过（测试 + 镜像构建）
4. 涉及行为变更请更新 `docs/CHANGELOG.md`

## 行为准则

保持技术讨论的尊重与克制。我们欢迎尖锐的技术质疑，不接受人身攻击。
