# Discord AI Bot

[中文版](#中文) | [English Version](#english)

---

<a name="中文"></a>
## 🤖 Discord AI Bot (中文)

这是一个基于 `discord.py` 并且集成了 Google Gemini AI 的多功能 Discord 机器人程序。它采用模块化设计（Cogs），实现了 AI 问答、日常新闻推送、汇率查询以及智能链接总结等实用功能。

### 🌟 功能特性 (Features)

*   **模块化架构**：通过 Discord Cogs 实现各个功能的解耦，易于扩展和维护。
*   **AI 智能问答**：集成 Google Gemini API，可以使用 `/ask` 命令向 AI 提问。
*   **链接自动总结**：自动识别聊天中的网页链接，并生成内容摘要（开发中）。
*   **每日资讯推送**：
    *   每天早上 8 点（多伦多时间）自动推送每日新闻摘要。
    *   每天早上 9 点（多伦多时间）自动推送 AI 领域最新资讯。
*   **实用生活助手**：
    *   `/fx`：一键查询加元 (CAD) 对美元 (USD) 和人民币 (CNY) 的实时汇率。
    *   `/recipe`：输入现有食材，AI 为你生成推荐菜谱。
*   **硬核极客工具 (Dev Tools)**：
    *   `/explain`：遇到不懂的技术概念，让 AI 用最接地气的大白话和比喻解释给你听。
    *   `/vs`：一针见血对比两个技术栈（如 Vue vs React），给出极客推荐，终结技术圣战。
    *   `/regex`：输入人话描述，自动生成带详细图文解析的正则表达式。
    *   `/debug`：把错误日志扔给它，充当你的私人“代码医生”。
*   **机器人状态监控**：`/ping` 测试机器人连通性。

### 🛠️ 项目结构

```text
├── bot.py                # 机器人主入口文件，负责加载 Cogs 和同步命令
├── config.py             # 全局配置文件
├── .env.example          # 环境变量示例文件
├── requirements.txt      # Python 依赖清单
├── core/                 # 核心功能模块
│   └── ai_client.py      # 封装 Google Gemini API 请求
└── cogs/                 # Discord Cogs 模块目录
    ├── ai_daily.py       # AI 日报定时任务
    ├── ask.py            # /ask 问答斜杠命令
    ├── canada_life.py    # 加拿大生活助手（如 /fx 汇率查询）
    ├── dev_tools.py      # 硬核开发者工具（极客词典、技术对比、查bug）
    ├── lifestyle.py      # 生活方式助手（如 /recipe 菜谱生成）
    ├── link_summary.py   # 聊天链接自动总结监听器
    └── news_digest.py    # 每日新闻定时任务
```

### 🚀 快速开始

1. **克隆项目**并进入目录。
2. **安装依赖**：
   ```bash
   pip install -r requirements.txt
   ```
3. **配置环境变量**：
   将 `.env.example` 复制为 `.env`，并填入你的 Discord Bot Token 以及 Gemini API Key。
   ```env
   DISCORD_TOKEN=你的_DISCORD_BOT_TOKEN
   GEMINI_API_KEY=你的_GEMINI_API_KEY
   ```
4. **运行机器人**：
   ```bash
   python bot.py
   ```

---

<a name="english"></a>
## 🤖 Discord AI Bot (English)

This is a multi-functional Discord bot built with `discord.py` and integrated with Google's Gemini AI. It features a modular design using Discord Cogs and provides AI-powered Q&A, daily news feeds, currency exchange rates, and intelligent link summarization.

### 🌟 Features

*   **Modular Architecture**: Built with Discord Cogs for decoupled, easy-to-maintain, and extensible features.
*   **AI Q&A**: Integrated with Google Gemini API, allowing users to ask questions using the `/ask` command.
*   **Auto Link Summarization**: Automatically detects URLs in chat messages and provides AI-generated summaries (placeholder/in development).
*   **Scheduled Daily Digests**:
    *   Pushes a general news digest daily at 8:00 AM (Toronto Time).
    *   Pushes AI-specific news daily at 9:00 AM (Toronto Time).
*   **Lifestyle Utilities**:
    *   `/fx`: Instantly check the exchange rate of CAD to USD and CNY.
    *   `/recipe`: Input your available ingredients, and the AI will generate a recipe for you.
*   **Bot Status**: `/ping` command to check if the bot is responsive.

### 🛠️ Project Structure

```text
├── bot.py                # Main bot entry point; loads cogs and syncs commands
├── config.py             # Global configurations
├── .env.example          # Environment variables template
├── requirements.txt      # Python dependencies list
├── core/                 # Core functionality modules
│   └── ai_client.py      # Google Gemini API client wrapper
└── cogs/                 # Discord Cogs directory
    ├── ai_daily.py       # Scheduled task for daily AI news
    ├── ask.py            # /ask slash command for AI questions
    ├── canada_life.py    # Canada life utilities (e.g., /fx exchange rates)
    ├── lifestyle.py      # Lifestyle utilities (e.g., /recipe generation)
    ├── link_summary.py   # Listener for summarizing links in chat
    └── news_digest.py    # Scheduled task for daily general news
```

### 🚀 Quick Start

1. **Clone the repository** and navigate to the directory.
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure Environment Variables**:
   Copy `.env.example` to `.env` and fill in your Discord Bot Token and Gemini API Key.
   ```env
   DISCORD_TOKEN=your_discord_bot_token_here
   GEMINI_API_KEY=your_gemini_api_key_here
   ```
4. **Run the bot**:
   ```bash
   python bot.py
   ```
