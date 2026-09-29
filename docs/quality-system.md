# 质量系统运行手册

第三阶段把质量检查分成离线回归、真实端到端评测、运行时观测和 CI 四层。离线回归不需要模型凭据，真实评测只在受控环境中运行。

## 本地检查

```powershell
uv sync --locked --group dev
uv run ruff check knowmind-server knowmind-agent knowmind-eval knowmind-mcp
uv run pytest knowmind-server/tests knowmind-eval/tests knowmind-mcp/tests -q
uv run python knowmind-eval/pipelines/run_eval.py --mode offline --fail-under 0.60

npm ci
npm --workspace knowmind-web run lint
npm --workspace knowmind-web run test:coverage
npm --workspace knowmind-web run build
npx playwright install chromium
npm --workspace knowmind-web run test:e2e
npm audit --audit-level=high
```

前端覆盖率门槛目前针对高风险的认证、HTTP 和流式聊天服务设置，报告会明确列出范围；它不是整个页面目录的覆盖率声明。Playwright 核心流程使用 API mock，验证浏览器中的登录、流式答案和退出登录；真实模型/知识库评测由下面的 live workflow 执行。

## 真实评测

评测器的 `--mode live` 只向 `/api/v1/chat/stream` 发送问题，不把标准答案发送给服务端，消费完整 SSE（答案、引用、trace 和结束事件），然后写入带版本号的 JSON 报告。可选字段 `require_citations` 可把无引用答案判为失败。离线模式使用显式提供的答案和词面重叠指标，不能解读为语义正确率；RAGAS 需要额外配置 judge 模型。

GitHub Actions 的 **Live RAG evaluation** 是手动触发的。`evaluation` environment 需要配置：

- `EVAL_API_BASE_URL`：已部署 API 的地址
- `EVAL_EMAIL` / `EVAL_PASSWORD`：专用评测账号
- `EVAL_KB_ID`：评测知识库 ID（可选）

数据集放在 `knowmind-eval/datasets`，不要把生产凭据或真实用户数据提交到仓库。CI 会把报告作为 artifact 保存，并按 `--fail-under` 失败门槛退出。

## 观测和成本

请求会返回 `X-Request-ID`，并输出结构化请求日志。每次已认证的生成会写入 `ai_usage_events`，`GET /api/v1/observability/usage?days=7` 返回轮次、成功/失败/停止、估算 token、延迟、模型和 micro-USD 聚合。

这些 token 是服务端基于文本长度的估算，延迟只覆盖生成阶段；规划、嵌入、检索和工具循环的上游调用可能不在账本中，也不是供应商账单的权威值。部署前设置 `LLM_INPUT_COST_PER_MILLION_USD` 和 `LLM_OUTPUT_COST_PER_MILLION_USD` 后，UI 才会显示已配置的成本估算。

执行数据库迁移：

```powershell
uv run alembic -c knowmind-server/alembic.ini upgrade head
```

## 合并门禁

`.github/workflows/ci.yml` 在 push 和 pull request 上强制执行后端测试、评测门槛、前端 lint/覆盖率/构建、Playwright 和 Bandit/npm audit；`.github/workflows/codeql.yml` 负责 Python 与 JavaScript/TypeScript 扫描。仓库管理员还需要在 GitHub 的 branch protection/ruleset 中把这些 job 设为 required checks，并禁止绕过检查直接合并；workflow 文件本身不会自动开启分支保护。
