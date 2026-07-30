# Discord AI Bot 架构文档

## 1. 目标与原则

本项目是一个基于 `discord.py` 的低成本社区机器人。核心目标按优先级排列为：

1. 内容必须可追溯：新闻事实来自 RSS、Hacker News 或用户提供的网页，模型负责筛选和表达。
2. 免费额度可持续：限制输入规模、输出 token、并发和自动触发频率。
3. 故障可降级：单一模型或单一 RSS 源失败不应拖垮整个机器人。
4. 推送不重复：生成可以重试，Discord 发送采用 at-most-once 语义。
5. 小项目不过度框架化：基础设施集中在 `core/`，Discord 交互保留在 `cogs/`。

## 2. 模块结构

```text
/discord-bot
├── bot.py                     # Bot 生命周期、扩展加载、命令同步和全局错误处理
├── config.py                  # 根目录、时区、日志级别和环境变量
├── core/
│   ├── ai_client.py           # 按能力路由 AI provider 与 Gemini cooldown
│   ├── ai_providers.py        # OpenAI-compatible 请求与统一 AIResult
│   ├── feeds.py               # 异步 RSS 下载、UTC 时间过滤和并发容错
│   ├── jobs.py                # RetryPolicy、single-flight 和单次发送事务
│   ├── storage.py             # 带进程锁和原子替换的 JSON Store
│   ├── settings.py            # 公共设置/本地密钥分离
│   ├── news_cache.py          # 高级新闻缓存、批量去重和推送状态
│   ├── data_ingester.py       # 高级新闻数据源定义与标准化
│   ├── web_fetcher.py         # 网页大小/超时/跳转/内网访问限制
│   ├── web_search.py          # 固定来源检索、证据限量与来源链接
│   ├── logging_config.py      # 标准日志初始化
│   └── utils.py               # Discord Embed 和 Markdown 表格转换
├── cogs/
│   ├── ask.py                 # /ask
│   ├── help.py                # 动态 /help 命令目录
│   ├── link_summary.py        # 自动链接总结与 /summary
│   ├── ai_daily.py            # Hacker News / AI 日报
│   ├── news_digest.py         # 国际/加拿大/金融 RSS 日报
│   ├── advanced_news.py       # 小时抓取、AI 打分和早晚精读
│   ├── daily_reading.py       # 每日英文阅读
│   ├── health.py              # /health 管理员诊断
│   └── ...                    # 设置、生活和开发工具
├── scripts/
│   ├── healthcheck.py         # 零生成 token 健康检查
│   └── validate.py            # 编译 + 测试 + 健康检查
└── scratch/                   # 回归和集成测试
```

## 3. 启动生命周期

`bot.py` 使用 `DiscordBot.setup_hook()` 加载 Cog 和同步 slash command。`setup_hook` 每个进程只执行一次；Discord Gateway 重连只触发 `on_ready` 日志，不会重新加载 Cog 或重复启动定时循环。

加载失败的扩展会被单独记录，其他扩展仍可启动。全局 app command error handler 统一处理权限、冷却和未知异常，已经响应过的 interaction 会使用 follow-up 返回错误。

全局时区来自 `BOT_TIMEZONE`，默认 `America/Toronto`，所有定时 Cog 使用 `config.TZ`，不再各自创建时区对象。

## 4. AI 服务层

### 4.1 结果与 provider

`core.ai_providers.AIResult` 保存：

- `text`：正文；
- `provider`：Gemini、Groq、Zhipu 或 OpenRouter；
- `model`：实际模型 ID。

Groq、智谱和 OpenRouter 都使用 OpenAI-compatible Chat Completions 协议，因此共享 `request_openai_compatible()`。该函数负责：

- system/user message 构造；
- 模型顺序降级；
- JSON mode；
- 最大输出 token；
- provider 级总超时、HTTP 错误和响应结构验证；
- 清除 `<think>` 推理块。

`ask_ai()` 保留原有字符串 API，使用内部 HTML comment 携带模型 attribution，保证旧 Cog 和测试脚本兼容。Embed 层会移除 comment 并生成 `Powered by ...` footer。

### 4.2 能力路由与降级顺序

```text
普通生成（use_search=False）
  Groq: qwen/qwen3.6-27b
        → openai/gpt-oss-120b
        → openai/gpt-oss-20b
         ↓
  Zhipu: glm-4.7-flash
          → glm-4.5-flash
         ↓
  OpenRouter 免费节点池
         ↓
  Gemini offline（最后兜底）

通用联网生成（use_search=True）
  Gemini Search
    ├─ 非限流失败且允许离线 → Gemini offline（一次）
    └─ 失败/cooldown/未配置
         ↓
  Groq → Zhipu → OpenRouter
```

普通问答、新闻 JSON 打分、日报整理和英文阅读都不需要模型自行联网，因此优先使用 Groq
的 Qwen，把 Gemini 免费额度留给明确开启 Search 的请求。`fallback_offline=False` 的
联网请求不会伪装成普通离线回答；Gemini Search 不可用时会直接报告联网服务不可用。

`/ask` 使用一个可选的 `mode` 下拉参数，默认不消耗联网配额：

```text
Qwen 普通问答（默认）
  └─ 普通生成链：Qwen → Zhipu → OpenRouter → Gemini offline

Qwen 网页检索（低成本）
  ├─ Google News RSS（最多 3 条）
  └─ 中文 Wikipedia API（最多 2 条）
       ↓
     固定大小证据包（[S1]...，含原始 URL）
       ↓
     普通生成链，Qwen 优先
       ↓
     程序确定性附加“来源”链接

Gemini 原生搜索
  └─ Gemini Search（fallback_offline=False）
```

Qwen 网页检索与 Gemini 原生搜索互不自动切换：两个抓取源都无结果时，前者提示用户
重试或主动选择 Gemini，不会自动花费 Google 配额；Gemini Search 不可用时也不会伪装
成离线回答。新闻和百科事实来自抓取结果，模型只做归纳；网页材料被明确标记为不可信
数据，不能覆盖 system instruction。Embed footer 始终显示实际 provider/model，基础
生成链发生故障降级时不会把备用模型冒充成 Qwen。

OpenRouter 当前内置节点：

1. `nvidia/nemotron-3-super-120b-a12b:free`
2. `nvidia/nemotron-3-ultra-550b-a55b:free`（仅普通文本）
3. `openai/gpt-oss-20b:free`
4. `nvidia/nemotron-nano-9b-v2:free`

模型目录在 2026-07-30 通过官方 API 实时验证。`scripts/healthcheck.py --live` 会重新验证列表，避免长期依赖文档中的静态状态。OpenRouter 列表不含 Google 节点；JSON mode 会跳过不支持 `response_format` 的 Ultra，然后继续尝试 GPT-OSS 和 Nano。

Qwen 3.6 在本项目中使用非思考模式，并要求 Groq 只返回最终答案；GPT-OSS 采用 low reasoning；智谱 GLM-4.7/4.5 Flash 都关闭 thinking。这些设置避免基础分类和摘要的推理过程占满 completion token 预算。Groq 已公告 `llama-3.3-70b-versatile` 将于 2026-08-16 下线，因此不再把它列为候选。OpenAI-compatible 接口若返回 `finish_reason=length`，会将该候选视为失败并切换到下一个模型，不会把不完整正文交给 Discord 或 JSON 解析器。

Gemini Search 和最后兜底固定使用稳定版 `gemini-3.6-flash`。该模型于 2026-07-21 GA；相较 3.5 Flash，官方定位是更强的复杂任务表现、更少的 token/轮次和更低价格。它不参与普通任务的首选链路。

### 4.3 Cooldown 与失败语义

Gemini 出现 `429` 或 `RESOURCE_EXHAUSTED` 时记录服务级 cooldown。冷却期内联网请求直接进入备用 provider，普通请求则继续沿非 Google provider 顺序执行，不再尝试 Gemini offline。

普通交互命令在所有 provider 失败时得到用户友好的错误文本；定时内容使用 `raise_on_failure=True`，让失败进入任务重试，不会把错误提示作为日报正文推送。

所有请求默认限制 4096 个输出 token。高级新闻以最多 8 条一批、3000 输出 token 进行 JSON 打分，确保免费模型的 prompt 与 completion 预算不会因单次请求超过 8k；Discord 最终正文仍限制在 4000 字符附近。

OpenAI-compatible provider 的超时覆盖整个候选模型池，而不是每个模型重新计时；HTTP 413 会直接终止该 provider 的模型轮询，因为相同 payload 不会因切换模型而缩小。

## 5. 定时任务事务

`core.jobs` 集中实现两个概念：

- `retry_async()`：默认最多 3 次，延迟按 60s → 120s 指数退避，上限 300s。
- `run_delivery_job()`：同一进程内 single-flight；只重试 build，deliver 仅执行一次，发送后状态更新也不会导致重新发送。

标准日报流程：

```text
定时/管理员触发
      ↓
asyncio.Lock（已有实例则跳过）
      ↓
抓取 + AI 生成（可重试）
      ↓
Discord channel.send（单次）
      ↓
可选 on_delivered（例如清理新闻缓存）
```

这里选择 at-most-once 而不是“发送失败就重试”。Discord 已接收消息但客户端超时属于不确定状态，自动重发会产生用户之前遇到的双报告。代价是极少数不确定发送可能漏报，可由管理员手动测试命令补发。

每日英文阅读包含三张卡片，使用自己的 single-flight 锁；每张卡片生成最多重试一次，发送成功后 reaction 失败不会重发卡片。

## 6. 新闻与 RSS 数据流

`core.feeds` 统一所有 RSS：

1. `aiohttp` 下载，20 秒总超时、单源最大 5 MB；
2. `feedparser` 在线程中解析 bytes；
3. RSS 时间按 UTC (`calendar.timegm`) 解释；
4. 单源失败只记录 warning，其他源继续；
5. 输出统一 `FeedItem`。

低成本输入限制：

- AI/HN 日报读取 Top 30；
- 综合日报每个 RSS 最多 8 条；
- 高级新闻每个 RSS 最多 4 条；
- 高级新闻在送入模型前使用一次缓存读取批量去重。

高级新闻的 60 分钟 interval loop 会跳过进程启动时的即时执行，避免重启触发全量补抓；手动测试命令仍可立即抓取。同一进程只允许一个抓取任务运行。若所有 AI provider 都失败，本轮立即停止，不再用剩余批次持续冲击限流节点。

高级新闻 JSON 分析直接调用失败即抛异常的 `generate_ai()`，不会经过可能返回用户提示文本的兼容接口。代码会校验 `news` 为数组，只接受原始 batch 中存在的 URL，恢复原始标题/来源，将分数限制在 0-1 后再写入缓存。启动时会移除缺少当前评分字段的旧 schema 缓存；这些记录无法参与现有筛选，且会阻止相同 URL 按新规则重新打分。

## 7. 链接总结

### 7.1 `/ask` 联网检索

`core.web_search.search_web()` 只访问代码内固定的 Google News RSS 和中文 Wikipedia
API，不接受用户提供目标主机。两者并发请求、共享 12 秒总超时，单个响应最大 1 MB；
查询最多 300 字符，最终保留最多 3 条新闻和 2 条百科摘要。解析结果再次校验 HTTPS
主机与路径，拒绝 Feed/API 中注入的第三方 URL。

检索材料按 `[S1]` 编号交给模型，并要求最新事实不得由模型记忆补齐。来源列表由程序
生成并为其预留 Discord Embed 字符预算，所以模型输出过长时优先截短回答而保留链接。
单一抓取源失败只记录 warning，另一个来源仍可完成回答。

### 7.2 指定链接总结

普通网页经 `core.web_fetcher.fetch_public_html()` 下载：

- 只允许 HTTP/HTTPS；
- 拒绝 URL 凭证、localhost、私网和保留 IP；
- 每次跳转重新验证，最多 3 次；
- 总超时 20 秒，正文最大 2 MB；
- 只接受 HTML/XHTML/plain text。

随后 `trafilatura` 在线程中提取正文，最多向模型提供 20,000 字符。自动监听每位用户 60 秒一次，整个 Cog 最多并发两个总结任务；`/summary` 同样共享并发上限。

YouTube 链接继续使用 `youtube-transcript-api` 获取字幕，不下载视频媒体。

## 8. 存储与密钥

`core.storage.JsonStore` 使用进程内 `RLock` 和同目录临时文件 + `os.replace`，避免写入中断造成半个 JSON 文件。

- `settings.json`：频道 ID、模型偏好等非敏感设置，可跟踪。
- `data/news_cache.json`：新闻缓存，Git 忽略。
- `data/secrets.json`：slash command 保存的本地密钥，Git 忽略。
- `.env`：部署密钥，Git 忽略。

`get_secret()` 优先读取本地 secret store，再读取环境变量，最后兼容旧版本曾写入 `settings.json` 的密钥。再次保存密钥时会删除旧的公共设置项。

## 9. 展示层

`create_ai_embed()` 负责：

- 提取 provider/model footer；
- 将 Markdown 表格确定性转换为项目符号；
- 保留代码块中的表格字符；
- 截断超出 Discord Embed description 限制的正文。

日报 prompt 同时要求 bullet list、禁止表格和禁止生成第二版。Prompt 是第一层约束，确定性转换是第二层兜底。

`/help` 不维护静态命令清单，而是在调用时读取 `bot.tree.get_commands()`；因此根级命令和
所有已加载 Cog 命令会随启动同步自动出现。命令按描述前缀分为常用、开发工具、管理员
和实验功能，单个字段遵守 Discord 的 1024 字符限制。帮助 Embed 仅对调用者可见，
列出管理员命令不会绕过命令本身的权限检查。

## 10. 运维与自动化

`/health` 是管理员专用、零模型调用的运行时诊断，展示：

- Gateway latency；
- provider 是否配置、Gemini 模型和 cooldown；
- 定时 Loop 是否运行/失败；
- 推送频道是否配置。

`scripts/healthcheck.py --strict --live` 不调用模型生成，只验证：

- Python/JSON/channel 配置；
- 至少一个 AI provider；
- Gemini key + model metadata；
- Groq/OpenRouter 实时模型目录；
- Discord bot token；
- BBC/NPR RSS 抓取；
- Google News/Wikipedia 联网问答抓取。

`scripts/validate.py` 依次执行 compileall、核心回归测试、所有 Cog 加载/卸载测试和严格健康检查。`--live` 可用于 cron、systemd timer 或 CI。

`.github/workflows/validate.yml` 在 push/pull request 上使用 Python 3.13 运行验证；CI 不持有部署密钥，因此使用 `--allow-missing-secrets`，密钥与在线 provider 检查留给部署环境的 live healthcheck。

## 11. 测试策略

测试脚本统一放在 `scratch/`：

- `test_core_services.py`：原子存储、密钥隔离、缓存去重/迁移、RSS UTC、退避、URL 安全、AIResult。
- `test_extensions.py`：所有 Cog 的加载/卸载，以及 `/help` 动态命令覆盖和分类。
- `test_regressions.py`：Gemini cooldown、provider fallback、高级新闻批次失败/启动行为、日报 single-flight/单次发送、表格转换。
- 其他 `test_*.py`：按 provider 或数据源进行人工集成测试。

## 12. 当前限制

- single-flight 与发送语义只覆盖单进程；多副本部署需要 Redis/数据库分布式锁和持久化 delivery key。
- JSON Store 适合个人/小社区机器人，不适合多进程高并发写入。
- 网页目标会在请求前验证 DNS 和每个 redirect；它降低常见 SSRF 风险，但不替代独立网络沙箱。
- 免费模型和 RSS 源会变化，应定期运行 live healthcheck。
