# Discord AI Bot 架构文档 (Architecture)

## 1. 系统概述 (System Overview)

本项目是一个基于 `discord.py` 构建的多功能 Discord 机器人。其核心亮点是通过整合 Google Gemini AI 模型，为 Discord 社区提供智能化的问答、信息总结和自动化工具。

整体架构采用了**模块化设计**，通过 Discord 原生的 `Cogs` 机制实现了功能的高度解耦，使得核心引擎与具体业务逻辑分离，便于扩展和维护。

## 2. 目录结构与职责 (Directory Structure)

```text
/discord-bot
├── bot.py                # 应用程序主入口
├── config.py             # 全局环境变量加载
├── .env                  # 敏感配置 (Token, API Key)
├── requirements.txt      # 依赖包列表
├── core/                 # 核心基础服务层 (Core Services)
│   ├── ai_client.py      # Gemini AI 客户端封装
│   ├── settings.py       # 本地 JSON 配置读写
│   └── utils.py          # 通用工具函数 (如统一消息卡片 Embed 生成)
└── cogs/                 # 业务逻辑模块层 (Discord Cogs)
    ├── ask.py            # AI 智能问答模块
    ├── link_summary.py   # 网页与视频总结监听与命令模块
    ├── ai_daily.py       # 定时任务：AI 日报推送
    ├── news_digest.py    # 定时任务：综合新闻推送
    ├── daily_reading.py  # 定时任务：每日英文阅读 (Scenario/RSS/TED)
    ├── canada_life.py    # 实用工具：汇率查询等
    ├── dev_tools.py      # 开发者工具：极客词典、正则生成等
    ├── lifestyle.py      # 生活方式助手：菜谱生成等
    └── settings.py       # 用户个性化设置管理
```

## 3. 核心层设计 (Core Layer)

### 3.1 机器人主引擎 (`bot.py`)
作为入口文件，它负责：
1. 初始化 `discord.ext.commands.Bot` 实例。
2. 配置必要的 Intents (如 `message_content`) 以读取用户消息。
3. 在 `on_ready` 事件中动态加载 `cogs` 目录下的所有模块。
4. 同步斜杠命令 (Slash Commands) 到 Discord 服务器。
5. 包含全局的基础连通性测试命令 (`/ping`)。

### 3.2 AI 客户端层 (`core/ai_client.py`)
采用单例模式封装 Google `genai` SDK，提供简单易用的异步接口：
- `reload_client()`: 支持动态加载和重新初始化 API 凭证，优先读取本地设置(`settings.json`)，回退到系统环境变量。
- `ask_ai(text, system, use_search)`: 核心请求方法，用于向 Gemini 发送提示词并获取结构化响应。支持通过 `use_search=True` 动态挂载 Google Search 工具以获取实时联网数据。

### 3.3 数据持久层 (`core/settings.py`)
提供轻量级的本地存储方案：
- 使用 `settings.json` 进行简单的数据持久化。
- 提供了 `get_setting` 和 `set_setting` 方法，用于动态管理应用级或用户级的偏好设置（如自定义的 API Key, 默认调用的 AI 模型名称等）。

### 3.4 视图构建层 (`core/utils.py`)
- 提供 `create_ai_embed` 工具，用于将 AI 文本输出包装成 Discord Embed 格式，使得 UI 更加美观，同时自动处理 Discord 限制的 4096 字符截断问题。

## 4. 业务模块层设计 (Cogs Layer)

功能被分散到多个独立的 Cog 中，主要包含以下三种交互模式：

1. **斜杠命令 (App Commands)**
   - 例: `cogs/ask.py` 中的 `/ask` 命令。响应用户主动触发的请求，经由 `ask_ai` 获取解答（默认关闭在线搜索以节省配额）。
   - 例: `cogs/lifestyle.py` 中的 `/recipe`，支持 `search_online` 参数动态决定是否联网搜索食谱（默认关闭）。

2. **开发工具与信息处理型**：
   这类功能通常需要处理特定的数据或文本结构，并不需要泛泛的聊天能力。
   - `dev_tools.py`：包含解释代码 (`/explain`)、生成正则表达式 (`/regex`)、工具对比 (`/vs`) 等程序员实用工具。
   - `link_summary.py`：利用 `trafilatura` 和 `lxml_html_clean` 解析任意网址正文，调用大模型进行网页总结。

3. **定时与自动化广播型**：
   使用 `discord.ext.tasks` 进行后台循环。
   - `ai_daily.py`：每日固定时间从 Hacker News 爬取 Top 30 热门帖子，交由大模型过滤筛选并生成每日的“AI 前沿快报”。
   - `news_digest.py`：利用 RSS 爬虫技术 (`feedparser`) 从多个高质量且中立的新闻源 (BBC World, CBC Top Stories, WSJ Markets) 并发抓取过去24小时内的国际、加拿大和金融新闻，交由离线大模型严格按照板块进行总结，保证了极高的新闻密度和真实性。
   - `daily_reading.py`：每天早上 7:30 自动生成并推送 3 种不同风格的英文阅读材料（AI 生成实用场景对话、真实 RSS 外刊精读、TED 金句赏析），帮助社区成员培养语感。

## 5. 数据流向与工作流程 (Data Flow)

以**链接自动总结**功能为例，数据流转如下：

1. **触发**: 用户在频道内发送包含 URL 的消息。
2. **拦截**: `link_summary.Cog.on_message` 监听到消息，匹配 URL。
3. **抓取 (IO 密集型)**: 
   - 判定为 YouTube 链接 -> 请求 `youtube_transcript_api` 获取字幕。
   - 判定为普通网页 -> 请求 `trafilatura` 抓取并提取正文。
   *(此过程通过 `asyncio.to_thread` 放到后台线程执行，避免阻塞 Discord 机器人的事件循环)。*
4. **AI 推理 (网络密集型)**: 抓取到的文本数据送入 `core.ai_client.summarize()`。
5. **UI 渲染**: 返回总结文本后，经 `core.utils.create_ai_embed()` 渲染为富文本卡片。
6. **响应**: 回复用户所在的频道，并替换原始占位的 "正在抓取" 消息。

## 6. 第三方依赖 (External Dependencies)

- **Discord 集成**: `discord.py`
- **网络请求**: `aiohttp` (用于异步并发获取 Hacker News 等外部 API 数据)
- **AI 引擎**: `google-genai` (用于调用 Google Gemini 3.5 Flash/Pro 等模型)
- **网页解析**: `trafilatura`
- **视频解析**: `youtube-transcript-api`
- **环境变量**: `python-dotenv`

## 7. 架构决策与历史尝试 (Architectural Decisions & Experiments)

为了确保机器人的高可用性与低成本，在开发过程中我们进行过以下技术路线的尝试与回滚：

1. **Google Search Grounding (联网搜索功能)**
   - **尝试**：最初在 `/news`（早间新闻）与 `/ask` 等核心命令中默认开启了 `use_search=True`。期望利用 Gemini 原生的 Google Search 工具直接获取实时数据并总结分类。
   - **回滚原因**：Google Gemini Free 额度（15 RPM / 1500 RPD）对 Search 调用的限制极为严苛，极易触发 `429 RESOURCE_EXHAUSTED` 甚至 `503` 宕机。此外，在配合强限制的提示词（如“必须分为4个板块，每板块5条”）时，Search 模式经常由于找不到完美匹配而发生**安全拦截幻觉**，直接返回空字符串（表现为 Discord 卡片内容为空）。
   - **最终决策**：将所有常规命令的搜索默认关闭。新闻模块 (`news_digest.py`) 则完全弃用 Search，转为使用传统的 RSS 爬虫 (`feedparser`) 获取多源高质量新闻 (如 BBC, Global News, WSJ)，再交由离线模型纯文本总结。稳定性提升至 100%。

2. **xAI SDK (Grok) 降级方案**
   - **尝试**：为了应对 Gemini 的 `429` 限流，最初计划接入 Elon Musk 的 Grok 模型作为二级回退网关。编写了集成代码并安装了 `xai-sdk`。
   - **回滚原因**：经过文档与 API 调研，确认 Grok 没有真正意义上的“免费测试额度”，必须绑定信用卡预充值 (Pre-paid Billing) 才能使用。这与本项目“零成本/纯免费白嫖”的运维理念不符。
   - **最终决策**：卸载 `xai-sdk`。引入 **OpenRouter** 平台（采用原生 `aiohttp`，0 额外依赖），使用其提供的海量免费节点（如 `google/gemini-2.5-flash-exp:free`）完美实现了免费兜底。
