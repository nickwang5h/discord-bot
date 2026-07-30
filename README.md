# Discord AI Bot

一个以低成本、可降级和可维护性为优先的 `discord.py` 机器人。它提供 AI 问答、链接总结、新闻日报、AI/Hacker News 日报、英文阅读和实用工具。

## 主要能力

- 按能力路由：`/ask` 可选普通 Qwen、Qwen 网页检索或 Gemini 原生搜索；默认使用低成本 Qwen。
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
│   └── validate.py           # 编译、测试、健康检查总入口
└── scratch/                  # 回归与集成测试
```

更完整的数据流和架构决策见 [`arch.md`](arch.md)。

## 安装

要求 Python 3.10+。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

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

BOT_TIMEZONE=America/Toronto
LOG_LEVEL=INFO
```

启动：

```bash
python bot.py
```

## 配置与密钥

- `settings.json`：频道 ID、模型偏好等非敏感设置，可提交到 Git。
- `.env`：部署环境密钥，已被 Git 忽略。
- `data/secrets.json`：通过 `/set_gemini_key` 保存的本地密钥，已被 Git 忽略。
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

在线检查只读取 Discord 身份、模型目录和测试 RSS，不执行模型生成，因此不会消耗 LLM token。脚本会用退出码表示成功或失败，适合 cron、systemd timer 或 CI：

```cron
15 6 * * * cd /path/to/discord-bot && /path/to/python scripts/healthcheck.py --strict --live >> healthcheck.log 2>&1
```

仓库还包含 `.github/workflows/validate.yml`，push 和 pull request 时会在无部署密钥的环境中运行同一套编译与测试。使用的是官方当前主版本 `actions/checkout@v6` 和 `actions/setup-python@v6`。

## 主要命令

- `/ask`：AI 问答，下拉选择 `Qwen 普通问答`、`Qwen 网页检索` 或 `Gemini 原生搜索`。
- `/help`：动态列出当前加载的全部 slash commands，并区分常用、开发、管理员和实验功能。
- `/summary`：总结网页或 YouTube 字幕。
- `/recipe`：按已有食材生成菜谱。
- `/fx`：查询 CAD 对 USD/CNY 汇率。
- `/explain`、`/vs`、`/regex`、`/debug`：开发者工具。
- `/test_news`、`/test_ai_news`、`/test_reading`：管理员手动测试定时内容。

自动链接总结对每位用户有 60 秒冷却，并限制同时执行数量，避免频道刷屏和免费额度被瞬间耗尽。
