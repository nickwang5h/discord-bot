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
├── AGENTS.md                  # 跨工具共享的规范与验证入口
├── .agents/
│   ├── rules/project-guidance.md  # Antigravity 到根 AGENTS.md 的桥接
│   └── skills/                # 架构维护与实时外部事实验证 Skill
├── .editorconfig              # 编辑器格式基线
├── .gitattributes             # 跨平台换行约束
├── .python-version            # 本地、CI 和部署统一使用 Python 3.13
├── requirements.txt           # 直接依赖范围
├── requirements.lock          # 带 hash 的完整可复现依赖图
├── bot.py                     # Bot 生命周期、扩展加载、命令同步和全局错误处理
├── config.py                  # 根目录、时区、日志级别和环境变量
├── core/
│   ├── ai_client.py           # 按能力路由 AI provider 与 Gemini cooldown
│   ├── ai_providers.py        # OpenAI-compatible 请求与统一 AIResult
│   ├── feeds.py               # 异步 RSS 下载、UTC 时间过滤和并发容错
│   ├── jobs.py                # RetryPolicy、single-flight 和单次发送事务
│   ├── storage.py             # 带进程锁和原子替换的 JSON Store
│   ├── settings.py            # 公共设置/本地密钥分离
│   ├── runtime_env.py         # WSL canonical owner-only env loader
│   ├── news_cache.py          # 高级新闻缓存、批量去重和推送状态
│   ├── data_ingester.py       # 高级新闻数据源定义与标准化
│   ├── weather.py             # 异步 wttr.in + Open-Meteo 备用天气抓取与 Embed
│   ├── web_fetcher.py         # 网页大小/超时/跳转/内网访问限制
│   ├── bilibili_transcript.py # 仅供旧镜像回滚/离线回归，不在当前 /summary 路径
│   ├── info_curator_client.py # 内部视频总结 sidecar 的严格 HTTP 客户端
│   ├── video_summary_worker.py # 参数数组调用 owner CLI 的 sidecar 网关
│   ├── web_search.py          # 固定来源检索、证据限量与来源链接
│   ├── logging_config.py      # 标准日志初始化
│   └── utils.py               # Discord Embed 和 Markdown 表格转换
├── cogs/
│   ├── ask.py                 # /ask
│   ├── help.py                # 动态 /help 命令目录
│   ├── link_summary.py        # 自动链接总结与 /summary
│   ├── ai_daily.py            # Hacker News / AI 日报
│   ├── news_digest.py         # 国际/加拿大/财经/科技与 AI RSS 日报
│   ├── advanced_news.py       # 小时素材采集和早晚探索阅读
│   ├── daily_reading.py       # 每日英文阅读
│   ├── weather.py             # 每日天气定时播报与 /weather 查询
│   ├── health.py              # /health 管理员诊断
│   └── ...                    # 设置、生活和开发工具
├── scripts/
│   ├── healthcheck.py         # 零生成 token 健康检查
│   ├── deploy_vps.sh          # 兼容的一键部署入口
│   ├── vps.sh                 # 部署、状态、日志、健康、配置和回滚 CLI
│   └── validate.py            # 编译 + 跟踪的行为测试 + 健康检查
├── docs/vps-deployment.md     # VPS 日常操作与故障处理说明
├── tests/                     # clean clone 和 CI 必跑的离线行为回归
└── ops/vps/                   # Dockerfile、Compose、远端部署与镜像回滚脚本
```

## 3. 启动生命周期

`bot.py` 使用 `DiscordBot.setup_hook()` 加载 Cog 和同步 slash command。`setup_hook` 每个进程只执行一次；Discord Gateway 重连只触发 `on_ready` 日志，不会重新加载 Cog 或重复启动定时循环。

加载失败的扩展会被单独记录，其他扩展仍可启动。全局 app command error handler 统一处理权限、冷却和未知异常，已经响应过的 interaction 会使用 follow-up 返回错误。

全局时区来自 `BOT_TIMEZONE`，默认 `America/Toronto`，所有定时 Cog 使用 `config.TZ`，不再各自创建时区对象。`BOT_ENABLE_SCHEDULED_JOBS=false` 时，定时 Cog 仍会加载管理员手动命令，但不会启动任何自动 loop；首次 VPS canary 使用这一模式防止误推送。`BOT_RELEASE` 由部署层注入三仓库 SHA manifest 的短 hash，并出现在 ready 日志和 `/health` 中。

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
  Groq: qwen/qwen3.8-27b
        → qwen/qwen3.6-27b
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

普通问答、新闻 JSON 选编、日报整理和英文阅读都不需要模型自行联网，因此优先使用 Groq
的 Qwen，把 Gemini 免费额度留给明确开启 Search 的请求。`fallback_offline=False` 的
联网请求不会伪装成普通离线回答；Gemini Search 不可用时会直接报告联网服务不可用。

`/ask` 使用一个可选的 `mode` 下拉参数，默认不消耗联网配额：

```text
Qwen 普通问答（默认）
  └─ 普通生成链：Qwen → Zhipu → OpenRouter → Gemini offline

Qwen 网页检索（低成本）
  ├─ Qwen 生成一个等价英文检索词（原问题保持不变）
  ├─ Google News RSS（原问题最多 3 条、英文查询最多 25 条）
  └─ 中英文 Wikipedia 搜索命中摘要（中文最多 2 条、英文最多 10 条）
       ↓
     最多 40 条双语候选证据（[S1]...，不向模型传入 URL）
       ↓
     普通生成链，Qwen 优先，统一用中文回答
       ↓
     程序附加模型实际引用的来源链接（最多 6 条）

Gemini 原生搜索
  └─ Gemini Search（fallback_offline=False）
```

Qwen 网页检索与 Gemini 原生搜索互不自动切换：两个抓取源都无结果时，前者提示用户
重试或主动选择 Gemini，不会自动花费 Google 配额；Gemini Search 不可用时也不会伪装
成离线回答。当前或可能变化的事实以抓取结果为准，模型可使用一般背景知识解释；网页材料
被明确标记为不可信数据，不能覆盖 system instruction。查询扩展由通用模型提示完成，
不包含按奖项、地点或年份编写的主题特判。Embed footer 始终显示实际 provider/model，基础
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

所有请求默认限制 4096 个输出 token。综合新闻选择和视野拾遗选编分别限制为 3000 输出 token；探索阅读每期最多 40 条候选，每条 RSS 证据最多 600 字符，只进行一次模型选编，不再每小时打分或二次改写。综合新闻另外受 Discord 单次消息 embed 总字符预算约束。

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
可选 on_delivered（例如记录已投递身份）
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
- 综合日报九个 RSS 每源最多 4 条（总候选最多 36，低于原来的 40），按发布方交错后筛选；
- 高级新闻每个 RSS 最多 4 条；
- 高级新闻在送入模型前使用一次缓存读取批量去重。

综合日报把发布方、标题、清洗后的 RSS 摘要和发布时间交给模型，但不交付链接。模型只返回
严格 JSON 中的候选 ID、简短中文标题和一两句中文摘要；程序校验 ID、类型、摘要及
渲染成本，再从原始候选恢复分类、发布方及 HTTPS 链接。链接和发布方不由模型生成；
中文表述仍依赖模型遵守 RSS 事实约束。选择不设固定条数或强制分类配额，按国际、加拿大、
财经、科技与 AI 的阅读需求选编，过滤重复报道、广告和泛泛评论。
国际源为 BBC World、NPR World、Al Jazeera，加拿大沿用 Global News，财经沿用
WSJ Markets、CNBC、Yahoo Finance，科技与 AI 使用 Ars Technica、TechCrunch。
多源不代表绝对中立，也不保证报道完整。程序按模型返回顺序保留每个发布方最多 3 条，
超额只略过、不补写；略过项仍做结构和引用校验，且不记入已投递历史。提示词要求争议说法
在标题和摘要中保留提出方与指控/声明限定，不把不同指控合并为事实，也不把转载当独立证据。

正式投递后的 URL、标题、RSS 摘要、内容指纹、发布方和分类保留 48 小时，最多 200 条。
下一轮排除 URL 与内容指纹都未变化、或只有标题和摘要大小写/空白变化的条目，再把最近
标题和摘要作为无链接历史上下文交给模型。摘要变化仍需模型判断是否包含实质进展；
跨链接的同事件判断也依赖模型，不能保证完全去重。过期记录在下一次写入时自动删除。
早报兼顾四个领域的概览，午报侧重新消息；提示词要求旧事件重发时写明“新进展”及新增事实。
输出使用国际、加拿大、财经、科技与 AI 分类 embed，中文标题链接到原始来源，但仍只调用一次
`channel.send()`；每个 description 低于 4096 字符，整条消息所有 embed 的文本总和保守限制
在 5800 字符内。`/test_news` 在当前频道忽略历史且不记录投递，用于观察完整候选路径而不
污染下一次正式摘要。

视野拾遗沿用 08:00 / 18:00 的出刊时刻及既有频道配置。60 分钟 interval loop 跳过启动时
即时抓取，只收集 RSS 原始素材，不调用模型；同一进程只允许一个抓取任务运行。
素材保留原始标题、摘要（存储最多 4000 字符）、发布方和发布时间。抓取窗口为 72 小时，
每源最多 4 条；出刊时排除超过 72 小时或缺少原始摘要的条目，按发布方交错提供最多 40 条候选。
无发布时间的素材使用采集时间判断是否过期，但展示明确标注日期未提供，不冒充新发布。

选编不使用评分、领域配额或“必须关联科技金融”的门槛；优先有具体案例、机制、观察或有据
反驳，普通读者能理解的意外发现。模型一次返回严格 JSON 的候选 ID、中文标题、事实概述和
阅读价值，最多 5 条（容量上限，不是目标数量），没有合适内容则不发。原文结论与编辑联想
在提示词中明确区分；只能依据 RSS，展示标明是摘要基础上的原文推荐，不声称已读全文。
程序恢复真实链接、发布方与日期，校验所有字段和整条长度后组装卡片，不截断半条新闻，
并且只在发送成功后标记实际展示的 URL。生成与事实判断仍依赖模型，离线测试不等于内容质量验证。

既有最多 150 条缓存继续保存已投递身份，按新条目自然淘汰；不在启动时删除旧评分记录。
没有原始 content 的旧模型摘要不参与选编，未投递条目在同 URL 再次抓取时补齐原始素材，
已投递条目保持原样。近期已推荐内容提供给模型作为事件去重参考；当前仍未实现与早午报/HN
的跨栏目事件去重，也未新增外部信息源。主题覆盖仍受既有 RSS 素材池约束。
管理员手动命令沿用原名称，通过相同采集/投递路径执行；手动投递也记录实际发送条目。

## 6.5 每日天气预报

`core.weather` 使用异步 `aiohttp` 提供零模型 Token 的天气数据抓取：

1. 优先请求 `https://wttr.in/{city}?format=j1&lang=zh`，超时 12 秒；
2. 若主接口失败，自动降级至 Open-Meteo Geocoding 与 Forecast 开放接口；
3. 解析今日高低温、体感、紫外线、降水概率、降水量、明日预报与基于天气状况的智能出行提醒（烟尘、雷暴、冻雨、大雪等警报）；
4. 格式化为 Discord Embed，根据严重警报、下雪、降雨、多云、晴朗匹配视觉色彩，并保留数据来源 Footer。

`cogs.weather` 在每日 07:00 (`America/Toronto`) 通过 `core.jobs.run_delivery_job` 发送预设城市（默认 `Ottawa, Sudbury`）的早间天气播报至 `WEATHER_CHANNEL_ID`（未指定时降级至 `NEWS_CHANNEL_ID`）。同时提供 `/weather [city]` 交互式查询命令，及 `set_weather_channel` / `/test_weather` 管理员配置与测试入口。

## 6.6 游戏特惠与 Epic 喜加一监控

`core.gaming` 与 `cogs.gaming` 提供零模型 Token 的自动化游戏降价与限免追踪：

1. **Epic 每周限免**：每周四 11:30 (`America/Toronto`) 定时轮询 Epic 官方促销接口，提取本周免费领取名单、原价、截止时间及下周预告；采用 ISO 周去重缓存，每周至多发送一次至 `GAMING_CHANNEL_ID`（未设置时降级至 `NEWS_CHANNEL_ID`）；提供 `/epic_free` 交互查询。
2. **Steam 愿望单降价监控**：每日 13:30 (`America/Toronto`) 通过免费 CheapShark REST API 轮询监控愿望单游戏，比对当前价格与历史最低价（`cheapestPriceEver`）；仅在游戏新打折或触及/打破历史史低时推送特惠 Embed；提供 `/deal <game>` 实时比价、`/watchlist` 查看愿望单、`/watch_game` 与 `/unwatch_game` 管理员增删接口。
3. **独立频道隔离**：提供 `/set_gaming_channel` 绑定独立 `#gaming` 频道，保持内容分流。

## 7. 链接总结

### 7.1 `/ask` 联网检索

`core.web_search.search_web()` 只访问代码内固定的 Google News RSS 和中英文 Wikipedia
API，不接受用户提供目标主机。请求共享 12 秒总超时，但不保存来源站点 Cookie，避免中英文
地区设置互相污染；单个响应最大 1 MB，查询最多 300 字符。原问题最多保留 3 条新闻，
英文等价查询最多保留 25 条新闻，中文百科最多 2 条、英文百科最多 10 条，总候选上限为 40。
Wikipedia 使用搜索词命中的片段而非一律截取条目开头，更容易覆盖名单、日期等实际所问信息。
解析结果再次校验 HTTPS 主机与路径，拒绝 Feed/API 中注入的第三方 URL。

Wikipedia 请求从 WSL canonical `/root/.config/discord-bot/runtime.env` 或部署环境读取 `BOT_CONTACT_EMAIL`，生成
`JonathanDiscordBot/1.0 (mailto:...)` User-Agent，以满足 Wikimedia 客户端身份要求。
邮箱只添加到 Wikipedia 单次请求，不发送给 Google News，也不写入日志、公开设置或
健康检查输出。缺失、占位、非法格式或包含换行时会跳过 Wikipedia，并记录不含邮箱值
的配置 warning；Google News 仍可独立工作。

检索材料按 `[S1]` 编号交给模型，但不把长 URL 送入提示词；模型最多引用 6 个最相关来源。
程序按引用编号恢复对应链接并为其预留 Discord Embed 字符预算，所以模型输出过长时优先
截短回答而保留链接；模型未引用任何编号时保底显示前三条。单一抓取源失败只记录 warning，
其他来源仍可完成回答。Google News 偶尔会按服务端策略缩减 RSS 条目，此时机器人只使用
实际返回的证据，不通过主题特判补造结果。

### 7.2 指定链接总结

普通网页经 `core.web_fetcher.fetch_public_html()` 下载：

- 只允许 HTTP/HTTPS；
- 拒绝 URL 凭证、localhost、私网和保留 IP；
- 每次跳转重新验证，最多 3 次；
- 总超时 20 秒，正文最大 2 MB；
- 只接受 HTML/XHTML/plain text。

随后 `trafilatura` 在线程中提取正文，最多向模型提供 20,000 字符。自动监听每位用户 60 秒一次，整个 Cog 最多并发两个总结任务；`/summary` 同样共享并发上限。

完整 B站 BV 链接与 YouTube 视频链接统一通过视频总结 sidecar 链路处理。Bot 先把输入收窄为
canonical `https://www.bilibili.com/video/BV...` 或 `https://www.youtube.com/watch?v=...`（拒绝未知参数和无效分P），然后通过
固定的 Compose 内网地址向 `core.info_curator_client` 发起一个无重试请求。客户端拒绝
外部 service host、redirect、超大响应和未知 envelope；同一 Bot 进程只允许一个视频
总结在 sidecar 中执行。

`core.video_summary_worker` 是 Python 3.12 sidecar 内的最小 HTTP 网关。它只接受一个
有界 URL，使用参数数组执行 `info-curator summarize-video --output ...`，严格解析 owner
CLI 成功/错误 envelope，并返回有界 Markdown 与 provider/model attribution。它不解析
字幕、不持有 Discord token、不接受任意命令，也不把 stderr、Cookie、provider response
或 artifact 路径返回 Bot。Info Curator 再通过 Media Transcriber CLI 获取和验证字幕（B站走 API，YouTube 走 Innertube fast path）；
Cookie、模型凭据、完整字幕和模型 quarantine 分别留在各 owner runtime/state。任何远程
模型失败都原样终止，不回退到 Bot 的 `ask_ai()`，避免对同一视频进行第二次隐式生成。
完整 Curator Markdown 是通用 sidecar 结果；Discord 专用 presenter 隐藏由 owner
渲染器生成、仅用于审计的独立 `引用：… seg-*` 行，再按连续字符边界把其余内容拆成多个
description 不超过 3900 字符的 embed。精简不修改 sidecar envelope 或持久 artifact，也不
匹配普通正文中的“引用”或一般链接；分段禁止调用通用 `create_ai_embed()` 的截断路径。

普通网页正文仍由 Bot 标记为不可信数据并最多向自身模型提供 20,000
字符。B站与 YouTube 视频输入隔离与逐条时间引用验证由 Info Curator/Media Transcriber 契约负责。

## 8. 存储与密钥

`core.storage.JsonStore` 使用进程内 `RLock` 和同目录临时文件 + `os.replace`，避免写入中断造成半个 JSON 文件。

默认未设置 `BOT_STATE_DIR` 时保持本地兼容布局；设置后必须是绝对路径，所有可变 JSON 都移到该根目录，代码 checkout 可只读更新：

- `<state-root>/settings.json`：频道 ID、模型偏好等非敏感运行设置；本地默认对应仓库中的 `settings.json`。
- `<state-root>/data/news_cache.json`：新闻缓存，Git 忽略。
- `<state-root>/data/news_digest_history.json`：综合新闻最近 48 小时投递身份，最多 200 条，Git 忽略。
- `<state-root>/data/secrets.json`：slash command 保存的本地密钥，Git 忽略。
- `/root/.config/discord-bot/runtime.env`：WSL canonical 私密配置；目录 0700、
  文件 0600，process-first，拒绝 symlink/unsafe mode；根 `.env` 仅在 canonical
  文件缺失时作为迁移 fallback；VPS 继续使用容器环境注入。

`BOT_CONTACT_EMAIL` 也存放在 canonical runtime env 或托管平台的私密环境变量中。虽然它不是 API
密钥，但属于运营者个人信息，仓库中的 `.env.example` 只保留空占位符。
B站 Cookie 不再进入 Discord secret store/runtime；它只存在于 Media Transcriber 的
owner-only runtime，并只在 sidecar 内的 Media Transcriber 子进程读取。Info Curator
provider key 同样使用独立 runtime，Bot 只收到已验证、无完整字幕的 Markdown 结果。

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
- provider、内部视频 sidecar 是否配置，以及 Gemini 模型和 cooldown；
- 定时 Loop 是否运行/失败；
- 推送频道是否配置。

`scripts/healthcheck.py --strict --live` 不调用模型生成，只验证：

- Python/JSON/channel 配置；
- 至少一个 AI provider；
- Wikipedia 联系邮箱是否有效（只报告状态，不显示值）；
- Gemini key + model metadata；
- Groq/OpenRouter 实时模型目录；
- Discord bot token；
- BBC/NPR RSS 抓取；
- Google News/Wikipedia 联网问答抓取。

`scripts/validate.py` 依次执行 compileall、仓库内 `tests/` 的离线行为回归和健康检查；`--live` 可用于人工在线验证。`scratch/` 仅用于本地实验，不参与 clean clone 的验收。

`.github/workflows/validate.yml` 在 push/pull request 上使用 Python 3.13，并通过 `requirements.lock` 的固定版本与 hash 安装依赖。CI 不持有部署密钥，因此使用 `--allow-missing-secrets`，密钥与在线 provider 检查留给部署环境的 live healthcheck。直接依赖仍声明在 `requirements.txt`，更新后必须用 uv 重建 lock 并重新验证。

VPS 运行形态是同一 Docker Compose 项目中的两个无公开端口服务：Python 3.13 Bot 与
Python 3.12 视频 sidecar。两者均固定 patch 版本和基础镜像 digest，以 uid/gid 1000、
只读根文件系统、drop-all capabilities、资源上限和轮转日志运行。Bot 只挂载 Discord
state；sidecar 只读挂载两套 owner runtime，并单独持久化 Info Curator artifacts。二者
仅使用默认 Compose 内网，均不加入 Caddy 的 `infra-edge` 网络。sidecar 先通过内部
healthcheck，Bot 才启动；Docker `unless-stopped` 负责进程和宿主机重启恢复。

`scripts/vps.sh` 是本地日常运维入口，从环境变量、mode `600` 的用户配置文件或 SSH alias 解析 Tailscale SSH 目标，提供部署、状态、离线健康、日志、远端密钥编辑、镜像列表和回滚；目标与密钥不写入仓库。`deploy_vps.sh` 仅保留为兼容的一键部署别名。

远端 `ops/vps/deploy.sh` 只接受 Discord Bot、Info Curator 与 Media Transcriber 三个
clean `main` checkout。三个完整 SHA 的 manifest hash 形成共同 release tag，分别构建
Bot/sidecar 镜像后串行切换；候选必须通过两个容器健康检查和 Discord Gateway ready
日志，否则恢复上一 manifest。`rollback.sh` 同时恢复仍存在的两个同 tag 镜像，并兼容
sidecar 引入前的 Bot-only 镜像；所有回滚均不改写持久 artifacts/state。部署仍是单 Bot
实例短暂停机切换，不采用多副本滚动发布。

Agent Toolkit 基线以根 `AGENTS.md` 为唯一项目契约，`.agents/AGENTS.md` 和
`.agents/rules/project-guidance.md` 只负责路由，不复制另一套规则。Toolkit 只拥有
`.gitignore` 中标记的 bootstrap block；项目原有的 `data/`、`scratch/` 和可跟踪 VS Code
配置规则继续由本仓库维护。WSL VS Code 终端通过跟踪的 `.vscode/zsh/.zshrc` 恢复用户
`ZDOTDIR`，避免 history/completion 状态写入仓库。

Personal Ops 使用项目 ID `discord-bot` 和规范路径 `/root/Projects/discord-bot` 观察 Git
元数据；它不替代本仓库测试、运行健康或部署证据。其生成 Markdown 是派生视图，不能从本
仓库任务中直接编辑。

## 11. 本地测试策略

`tests/` 保存 provider fallback、delivery、存储、网络边界、扩展加载、部署配置和 B站字幕等离线行为测试，`scripts/validate.py` 与 CI 始终执行。测试使用临时 `JsonStore` 和 mock，不写真实 state、不调用付费模型。`scratch/` 仍是被 Git 忽略的本地工作目录，只保留人工集成脚本和临时诊断，不能成为验收前提。

## 12. 当前限制

- single-flight 与发送语义只覆盖单进程；多副本部署需要 Redis/数据库分布式锁和持久化 delivery key。
- JSON Store 适合个人/小社区机器人，不适合多进程高并发写入。
- 网页目标会在请求前验证 DNS 和每个 redirect；它降低常见 SSRF 风险，但不替代独立网络沙箱。
- 免费模型和 RSS 源会变化，应定期运行 live healthcheck。
- B站 sidecar 当前只接受完整 BV 第一P链接；短链、分P、无字幕视频和 ASR 均 fail closed。
- Compose 健康检查能证明配置和进程容器状态，但不能独立证明 Discord Gateway 长期在线；部署额外检查 ready 日志，长期可用性仍需 `/health` 或外部告警观察。
