# Discord AI Bot

一个以低成本、可降级和可维护性为优先的 `discord.py` 机器人。它提供 AI 问答、链接总结、新闻日报、AI/Hacker News 日报、英文阅读和实用工具。

## 主要能力

- 按能力路由：`/ask` 可选普通 Qwen、Qwen 双语网页检索或 Gemini 原生搜索；默认使用低成本 Qwen。
- 低成本新闻：RSS/Hacker News 负责事实输入，模型只负责筛选和整理。
- 稳定日报：抓取与生成可以重试，Discord 发送至多一次；并发触发会自动跳过。
- 格式兜底：模型生成的 Markdown 表格会自动转换为 Discord 可读的项目符号。
- 安全链接总结：限制网页大小、请求时间和重定向次数，并拒绝本机/私网地址。
- 免费健康检查：`/health` 和脚本检查不调用模型生成，不消耗 LLM token。

## 项目结构

```text
├── bot.py                    # Bot 生命周期、Cog 加载、全局错误处理
├── config.py                 # 项目路径、时区、日志和环境变量
├── core/
│   ├── ai_client.py          # AI 能力路由、降级与 Gemini cooldown
│   ├── ai_providers.py       # OpenAI-compatible provider 公共实现
│   ├── feeds.py              # 带超时的统一异步 RSS 抓取
│   ├── jobs.py               # 重试、single-flight、单次发送事务
│   ├── storage.py            # 原子 JSON 存储
│   ├── settings.py           # 普通设置与本地密钥分离
│   ├── news_cache.py         # 高级新闻去重与缓存
│   ├── web_fetcher.py        # 安全、限量的指定网页抓取
│   └── web_search.py         # Google News/Wikipedia 检索与来源格式化
├── cogs/                     # Discord 命令与定时业务
├── scripts/
│   ├── healthcheck.py        # 零 token 配置/在线健康检查
│   └── validate.py           # 编译、可选本地测试、健康检查总入口
```

更完整的数据流和架构决策见 [`arch.md`](arch.md)。

## 安装

要求 Python 3.10+；本仓库开发、CI 和 VPS 镜像统一使用 Python 3.13。

```bash
python -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
cp .env.example .env
```

`requirements.txt` 保存直接依赖范围，`requirements.lock` 保存可复现的完整依赖图。修改直接依赖后使用 `uv pip compile requirements.txt --python-version 3.13 --universal --generate-hashes -o requirements.lock` 重新生成 lock，并运行完整验证。

至少配置 Discord token 和一个 AI provider：

```env
DISCORD_TOKEN=...

# 推荐：普通生成的首选服务（Qwen 优先）
GROQ_API_KEY=...

# 可选：Gemini Search 与普通生成的最后兜底
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.6-flash

# 可选备用服务
ZHIPU_API_KEY=...
OPENROUTER_API_KEY=...

# Wikipedia API 身份标识；只在部署环境填写，不要提交真实邮箱
BOT_CONTACT_EMAIL=your-email@example.com

# 可选；部分 B站字幕需要登录态，建议只使用专用低权限账号
BILIBILI_COOKIE=...

BOT_TIMEZONE=America/Toronto
LOG_LEVEL=INFO
BOT_ENABLE_SCHEDULED_JOBS=true
```

启动：

```bash
python bot.py
```

## 配置与密钥

- `settings.json`：本地默认的频道 ID、模型偏好等非敏感设置，可提交到 Git。
- `.env`：本地开发密钥，已被 Git 忽略；VPS 通过 Compose `env_file` 注入，不复制进镜像。
- `data/secrets.json`：通过 `/set_gemini_key` 保存的本地密钥，已被 Git 忽略。
- `BOT_STATE_DIR`：可选的绝对路径；设置后，`settings.json`、`data/secrets.json` 和 `data/news_cache.json` 全部从该目录读写，使部署代码和持久状态分离。
- `BOT_ENABLE_SCHEDULED_JOBS`：是否启动日报、阅读和高级资讯循环；关闭后管理员手动测试命令仍可使用。
- `BOT_CONTACT_EMAIL`：Wikimedia 要求的机器人联系方式，只随 Wikipedia API 请求发送；日志和健康检查不会显示其值。
- `BILIBILI_COOKIE`：可选的 B站登录态，建议使用专用低权限账号；只发送给固定 Bilibili API，绝不提交或写入日志。
- `/set_news_channel`、`/set_test_news_channel`、`/set_reading_channel`：设置推送频道。
- `/set_model`：切换 Gemini 模型。
- `/health`：管理员查看 provider、定时任务、频道和 cooldown 状态。

## 定时任务

默认时区由 `BOT_TIMEZONE` 控制，默认 `America/Toronto`。

| 任务 | 默认时间 |
| --- | --- |
| 每日英文阅读 | 07:30 |
| 高级精读早刊 | 08:00 |
| AI/HN 日报 | 08:15 |
| 综合新闻早报 | 08:45 |
| 综合新闻午报 | 15:30 |
| 高级精读晚刊 | 18:00 |
| 高级资讯抓取 | 每小时 |

## 自动检查

本地完整检查：

```bash
python scripts/validate.py
```

附加在线检查：

```bash
python scripts/validate.py --live
```

仓库内 `tests/` 的行为回归测试始终由验证脚本和 CI 执行；`scratch/` 只保留本地实验，不属于 CI 输入。

在线检查只读取 Discord 身份、模型目录和测试 RSS，不执行模型生成，因此不会消耗 LLM token。脚本会用退出码表示成功或失败，适合 cron、systemd timer 或 CI：

```cron
15 6 * * * cd /path/to/discord-bot && /path/to/python scripts/healthcheck.py --strict --live >> healthcheck.log 2>&1
```

仓库还包含 `.github/workflows/validate.yml`，push 和 pull request 时会在无部署密钥的环境中按 lock 安装依赖，并运行同一套编译与测试。使用的是官方当前主版本 `actions/checkout@v6` 和 `actions/setup-python@v6`。

## VPS Docker 部署

`ops/vps/` 提供固定 Python 3.13 基础镜像、无入站端口的 Compose 服务、日志轮转、资源/权限限制、健康检查和按 Git SHA 标记的回滚入口。部署状态位于 `/srv/discord-bot/runtime/state`，密钥环境文件位于 `/srv/discord-bot/runtime/runtime.env`（权限必须禁止 group/other 访问）。Bot 不连接 Caddy 或公共 `infra-edge` 网络。

首次 canary 应在 VPS 的 `runtime.env` 设置：

```env
BOT_ENABLE_SCHEDULED_JOBS=false
```

确认 Gateway、`/ping` 和手动功能后再改为 `true`。部署前必须先停止使用同一 Token 的本地实例，项目只支持单进程 at-most-once 语义。

完成一次性 SSH 目标配置后，日常部署只需：

```bash
./scripts/vps.sh deploy
```

同一入口还提供 `status`、`health`、`logs`、`env`、`images` 和 `rollback`。部署脚本拒绝 dirty checkout 和并行部署，执行 fast-forward 更新、缓存构建、容器健康检查与 Gateway ready 检查；失败时恢复上一镜像，且不会改写持久状态。完整配置、常用命令、密钥编辑和故障处理见 [`docs/vps-deployment.md`](docs/vps-deployment.md)。

## 主要命令

- `/ask`：AI 问答，下拉选择 `Qwen 普通问答`、`Qwen 网页检索` 或 `Gemini 原生搜索`。Qwen 检索会保留原问题，并由低成本模型补充一个等价英文查询；中英文材料统一整理为中文回答。
- `/help`：动态列出当前加载的全部 slash commands，并区分常用、开发、管理员和实验功能。
- `/summary`：总结网页、YouTube 字幕或 B站视频字幕。
- `/recipe`：按已有食材生成菜谱。
- `/fx`：查询 CAD 对 USD/CNY 汇率。
- `/explain`、`/vs`、`/regex`、`/debug`：开发者工具。
- `/test_news`、`/test_ai_news`、`/test_reading`：管理员手动测试定时内容。

自动链接总结对每位用户有 60 秒冷却，并限制同时执行数量，避免频道刷屏和免费额度被瞬间耗尽。
