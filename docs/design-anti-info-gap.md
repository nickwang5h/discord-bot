# 反信息差系统 · 第二、三阶段设计稿（v2）

作者：Opus 5.5（只读代码，未改仓库）　日期：2026-10-09
仓库：`/root/Projects/discord-bot`（分支 `anti-info-gap/sources`，HEAD `3e7e45d`，读稿时工作区干净）

**v2 改动**（按总指挥的两条补充）：
1. 阶段 2 的核心改成**反馈机制**：每条推送一键标“🆕 新知 / 👌 已知 / 🚫 不关心”，统计后回流到个人线路的选编，让推送偏向“不知道但该知道”。关键词追踪降级为可选的第二步，作为板块（Ottawa、Sudbury、投资、AI 和科技、general）下的细化。
2. 新增**前置任务 T1**：在 `core/ai_client.py` 加 Anthropic provider（官方 `anthropic` SDK，固定 `claude-opus-5-5`），只给个人线路用，用 Claude Max 订阅附带的 API 额度，月目标 ≤ $20；共享专题仍走免费链。

已读：`AGENTS.md`、`arch.md`、`.agents/skills/maintain-architecture/SKILL.md`、`core/news/**`、`core/inbox.py`、`cogs/inbox.py`、`cogs/news.py`、`core/jobs.py`、`core/storage.py`、`core/ai_client.py`、`core/web_search.py`、`cogs/ask.py`、`cogs/help.py`、`cogs/health.py`、`docs/news.md`、`tests/test_news_pipeline.py`、`tests/test_inbox.py`、`tests/test_extensions.py`、`ops/vps/Dockerfile`、`ops/vps/compose.yaml`、`requirements.*`、`/root/HANDOFF.md`「整改（10/05）」。Claude API 的参数、价格和错误类型取自 claude-api skill 的文档（缓存日期 2026-10-06）。

---

## 0. 总览

### 0.1 实施顺序

```
T1  Claude provider（前置）
T2–T4  反馈机制：存储与曝光同步 → 按钮/反应 Cog → 个人推送挂按钮
T5–T6  反馈回流：画像计算 → following 选编使用画像（走 Claude）
T7–T11 主题追踪（可选第二步，按板块细化）
T12–T16 知识回环：存储 → 同步 → /recall → 周报 → Cog
```

### 0.2 贯穿全稿的决定

- **共享内容一字不改**：`general`、`discovery`、`power-us-ca` 的选编、提示词、渲染、发送参数、频道、`NEWS_LIMITS` 全部不动；它们的 `generate_ai()` 调用不传 `route`，所以仍走 Groq → 智谱 → OpenRouter → Gemini。共享频道里 bot 不预置反应、不挂按钮、不回消息。
- **个人内容只进个人频道、只认所有者**：个人推送（`following` 订阅、追踪命中、周报、/recall）只发到个人频道，默认复用 `INBOX_CHANNEL_ID`（`following` 现在已投递到这里）。所有新命令和按钮回调都先做 `bot.is_owner()` 检查；新命令另加 `@app_commands.default_permissions(administrator=True)`，非管理员在客户端里看不到。
- **状态**：反馈、追踪、知识库各用一个新 SQLite 文件（`data/feedback.sqlite3`、`data/watch.sqlite3`、`data/knowledge.sqlite3`）；用户可编辑的配置用 `JsonStore`（`watches.json`）；Claude 用量用 `JsonStore`（`data/claude_usage.json`）。新闻素材池 `news.sqlite3` 只通过一个只读连接（`mode=ro`）读取，不改它的 schema 和版本门禁。
- **投递 at-most-once**：沿用新闻的做法：先持久化发送意图，再调用一次 `channel.send`，成功后记录消息 ID；出异常或重启时遗留的发送一律标“不确定”，永不补发。
- **调度**：只用各 Cog 自己的 `tasks.loop` 和 `core.jobs`，不加新容器，也不加系统定时器。
- **命令全部扁平命名**（如 `/watch_add`，不用 `app_commands.Group`）。原因是 `tests/test_extensions.py` 断言每个顶层命令以 `` `/{name}` `` 出现在 /help 里，Group 会渲染成 `` `/watch add` ``，那条断言会失败。
- **辅助代码不放 `cogs/`**：`bot.py` 会把每个 `cogs/*.py` 当扩展加载，没有 `setup()` 的文件会加载失败。所有者检查照 `cogs/inbox.py` 的写法在各 Cog 内联。
- **新 store 一律懒加载**，测试里 patch `cogs.<name>.STATE_ROOT`，不写真实 `data/`。

### 0.3 FTS5 可用性（阶段 3 用）

`ops/vps/Dockerfile` 的基础镜像是 `python:3.13.13-slim-bookworm`。官方 Python 镜像链接的是 Debian bookworm 的系统 `libsqlite3`（3.40.x），Debian 编译时开了 FTS5。这一点把握很高，但**没有在这个镜像里实测过**：本机 WSL 没有 Docker。本机 uv 的 Python 3.13 用的是 SQLite 3.53.1，已实测 FTS5 和 trigram 都可用。所以设计里加了运行时自检和降级（§5.8），也建议上线前做一次只读确认（§9 Q1）。中文检索不用 `trigram` 分词器，因为“储能”“关税”这类两字词在 trigram 下用不上索引。改为自己把中日韩文字切成重叠双字，再交给 `unicode61` 分词。

---

## 1. 前置：Anthropic provider（T1）

### 1.1 目标与边界

- `core/ai_client.py` 的能力路由里加一个 Claude provider，用官方 `anthropic` Python SDK 的 `AsyncAnthropic`（不用 OpenAI 兼容壳）。模型固定为 `claude-opus-5-5`。
- **只有带个人路由标签的调用走 Claude**，其余调用的行为和现在逐字节一致。
- Claude 不可用、额度耗尽、限流、超时、拒答、输出被截断时，**同一次调用内**降级到现有的 Groq → 智谱 → OpenRouter → Gemini offline 链，调用方无感。
- `ANTHROPIC_API_KEY` 走现有的 `settings.get_secret()`（本地 secret store → 环境变量；VPS 由 compose `env_file` 注入 runtime.env）。缺失时不注册 provider、不报 warning（只打一条 info 日志）。

### 1.2 接口改动（向后兼容）

```python
async def generate_ai(text, system=..., use_search=False, fallback_offline=True,
                      json_mode=False, max_output_tokens=4096,
                      route: str | None = None,          # 新增：个人路由标签
                      json_schema: dict | None = None,   # 新增：Claude 结构化输出用的 schema
                      ) -> AIResult
```

- 当 `route in CLAUDE_ROUTES`、`use_search=False`、provider 已注册、未在冷却/停用期、且预算允许时，先试 Claude，失败再走原链；否则直接走原链（原链的 `max_output_tokens`、`json_mode` 含义不变）。
- `CLAUDE_ROUTES` 是 `ai_client` 里的常量表，每条路由固定 effort、Claude 侧 `max_tokens`、输入字符上限、每日次数上限（§1.4）。表里没有的 route 一律不走 Claude（即使传了 route）。
- `ask_ai()` 不加 route：个人线路的机器输出和回答都走 `generate_ai()`（与 `/ask` 的检索回答一致）。

### 1.3 Claude 请求形态

```python
response = await claude.messages.create(
    model="claude-opus-5-5",
    max_tokens=route.max_tokens,                 # 含思考 token，见下
    system=system,
    messages=[{"role": "user", "content": text}],
    output_config={"effort": route.effort,       # "low" | "medium"
                   **({"format": {"type": "json_schema", "schema": json_schema}} if json_schema else {})},
)
```

- **不传 `thinking`**：Opus 5.5 默认 adaptive，`disabled` 和 `budget_tokens` 都会返回 400。思考深度只用 `output_config.effort` 控制（Opus 5.5 默认是 `medium`，所以每条路由都显式设置）。
- **思考 token 按输出计费，并计入 `max_tokens`**，所以 Claude 侧的 `max_tokens` 要比免费链的输出上限大（见 §1.4）。`display` 默认 `omitted`，返回的 thinking block 文本为空，取正文时只读 `type == "text"` 的 block。
- **机器可读输出**用 `output_config.format`（`json_schema`），不用 prefill（Opus 5.5 不支持 prefill，会 400）。schema 只能用 API 支持的子集：每个 object 都要 `additionalProperties: false` 和 `required`；不支持 `minLength/maxLength/minimum/maximum`、递归和复杂数组约束。长度、条数这类约束留给现有的本地校验器，它们照常运行，Claude 的结果不跳过校验。
- 每个需要 JSON 的个人调用方都提供 `json_schema`；免费链仍走 `json_mode=True` 加原有提示词约束。
- 不用流式：最大的 `max_tokens` 是 6,000，非流式就够了。
- 不开 prompt caching：各路由的 system 都远低于 Opus 的最小可缓存前缀，开了也不生效。
- 客户端：`AsyncAnthropic(api_key=..., max_retries=0, timeout=90.0)`，懒加载。`max_retries=0` 是为了让失败尽快降级到免费链，不在 Claude 上重试。

### 1.4 路由、预算与美元上限（Opus 5.5：输入 $4 / 输出 $20 每百万 token）

| 路由（`route`） | 用途 | effort | Claude `max_tokens` | 输入上限（字符） | 每日次数 | 最坏单次 | 典型单次 |
|---|---|---|---|---|---|---|---|
| `personal.following` | 个人订阅选编（含反馈画像） | medium | 6,000 | 30,000（约 1 万 token） | 4（2 次定时 + 2 次预览/手动） | $0.16 | 约 $0.08 |
| `watch.confirm` | 追踪命中确认 | low | 3,000 | 12,000（约 4 千 token） | 6 | $0.076 | 约 $0.03 |
| `watch.aliases` | `/watch_add expand` 别名建议 | low | 1,000 | 1,000 | 3 | $0.021 | 约 $0.01 |
| `recall.plan` | /recall 检索词规划 | low | 1,000 | 1,500 | 15 | $0.022 | 约 $0.008 |
| `recall.answer` | /recall 回答 | medium | 4,000 | 10,000（约 4 千 token） | 15 | $0.096 | 约 $0.04 |
| `review.summary` | 周报小结（默认关闭，§9 Q7） | low | 1,500 | 8,000 | 1/周 | $0.04 | 约 $0.02 |

**`CLAUDE_LIMITS`**（公开设置，有默认值，范围内可调）：

| 项 | 默认 | 范围 |
|---|---|---|
| `daily_usd` | 0.60 | 0.05–1.50 |
| `monthly_usd` | 19.00 | 1–60 |
| `daily_calls` | 30 | 1–80 |

- **预留 + 结算**：调用前按“最坏成本”预留。最坏成本 = 估算输入 token × $4/M + `max_tokens` × $20/M；输入估算偏保守，中日韩字符按 1.5 token/字，其余按 1 token/3 字符，再加上 system。如果“已结算 + 已预留 + 本次最坏 > daily_usd”，或者月度累计超过 `monthly_usd`，或者路由当日次数已满，本次直接走免费链。调用结束后按 `response.usage.input_tokens` 和 `output_tokens` 的实际值结算，并释放预留。进程中途崩溃时预留不释放，偏保守。日期按 UTC，月份按 UTC 自然月。
- **月度估算**：典型一天约为 following 2 × $0.08 + 追踪 4 × $0.03 + /recall 3 问 × $0.05 ≈ **$0.43/天，约 $13/月**。最坏情况被 `daily_usd` 硬卡在 $0.60/天，31 天约 $18.6，`monthly_usd` = $19 再兜底，**保证每月 ≤ $20**。超限当天剩余时间停用 Claude，走免费链。
- 用量存在 `<STATE_ROOT>/data/claude_usage.json`（`JsonStore.update()` 原子更新），字段为：`days[YYYY-MM-DD]` = {reserved_usd, spent_usd, calls, input_tokens, output_tokens, routes{route: calls}}、`months[YYYY-MM]` = spent_usd、`disabled_until`、`disabled_reason`、`cooldown_until`。只保留 40 天。

### 1.5 失败分类与降级（每次都在同一调用内转免费链）

| 情况 | 识别方式（SDK 类型化异常优先） | 处理 |
|---|---|---|
| 额度耗尽 | `anthropic.BadRequestError` 且消息含 “credit balance is too low”；或 `e.type == "billing_error"`（402） | `disabled_until` 设为下一个 UTC 日 00:00，原因记为“额度耗尽”；失败请求不计费，结算 $0 |
| 限流 | `anthropic.RateLimitError`（429） | `cooldown_until = now + retry-after`（缺失时 60 秒） |
| 过载/服务端 | `InternalServerError`、`APIStatusError` 且 `status_code >= 500`（含 529） | 本次降级，冷却 60 秒 |
| 网络/超时 | `APIConnectionError`、`APITimeoutError`、`asyncio.timeout(90)` | 本次降级 |
| 认证/权限/模型不可用 | `AuthenticationError`、`PermissionDeniedError`、`NotFoundError` | 停用到下一个 UTC 日并打 error 日志（不打印密钥） |
| 其他 400 | `BadRequestError`（非额度） | 本次降级并打 error 日志（多半是 schema 写错，要修代码） |
| 拒答 | `stop_reason == "refusal"` | 本次降级，按实际 usage 结算 |
| 截断 | `stop_reason == "max_tokens"` | 本次降级，按实际 usage 结算（与现有 `finish_reason=length` 视为失败一致） |
| JSON 解析失败 | 有 schema 但正文不是合法 JSON | 本次降级 |

**`/health`**：AI Providers 段加一行，格式如 `- Claude Opus 5.5: ✅ 可用 · 今日 $0.21/$0.60 · 7 次 · 本月 $4.80/$19`，按状态显示“未配置 / 冷却 Ns / 额度耗尽至 HH:MM UTC / 今日预算已满”。`get_provider_status()` 增加 `claude` 和 `claude_usage` 两个字段（只给数字和状态，不含密钥）。`scripts/healthcheck.py` 的离线检查只报告“已配置/未配置”，不做在线调用；`--live` 可选地调一次 `client.models.retrieve("claude-opus-5-5")`，这个接口不产生生成费用。

**服务端拒答回退（`fallbacks`）**：claude-api skill 对 `claude-opus-5-5` 的默认做法，是开启服务端 `fallbacks: "default"`（beta `server-side-fallback-2026-07-01`）。**本设计默认不开**，原因有二：本地已有免费链兜底；服务端回退由其他模型按它自己的价格计费，会让美元预留算不准。新闻和个人收藏内容触发安全拒答的概率很低。是否改为开启，列为 §9 Q14。

### 1.6 依赖

- `requirements.txt` 加 `anthropic>=1,<2`（1.x 基于 `httpx2`，与现有 `httpx` 包并存）。实施时先按 `verify-realtime-data` 核实 PyPI 当前 1.x 版本。
- 用仓库原来的命令重建锁文件（锁文件头部有记录）：`uv pip compile requirements.txt --python-version 3.13 --universal --generate-hashes --output-file requirements.lock`。不加 `--upgrade`，uv 会保留已有 pin。
- 验收：`git diff requirements.lock` 只新增 `anthropic` 及其新依赖，所有已有包的版本和 hash 不变；`pip install --require-hashes -r requirements.lock` 在干净的 3.13 venv 里成功；`scripts/validate.py --allow-missing-secrets` 通过。
- `.env.example` 加空占位 `ANTHROPIC_API_KEY=`。VPS 上需要用户（或获得运维授权的人）把密钥写进 `/srv/discord-bot/runtime/runtime.env`，这一步不在代码任务内（§9 Q13）。

### 1.7 测试（离线，不调真实 API）

`tests/test_claude_provider.py`：patch 客户端工厂，返回假的 `messages.create`（结果对象用 `SimpleNamespace(content=[...], stop_reason=..., usage=..., model=...)`；异常用 SDK 的类型化异常构造，例如 `anthropic.BadRequestError(msg, response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)`）。覆盖以下情况：

- 无 route 或未知 route：不调用 Claude，与原链行为一致（用现有 provider 测试做回归）。
- 未配置密钥：provider 不注册，不 warning。
- 请求参数：不含 `thinking`；`output_config.effort` 与路由表一致；有 schema 时带 `format`；没有 assistant prefill。
- 额度耗尽 / 402：转免费链，当日停用，结算 $0。
- 429 有 retry-after / 无 retry-after 两种冷却；5xx、超时、拒答、`max_tokens`、JSON 不合法都转免费链。
- 预留与结算：超出 `daily_usd`、`monthly_usd`、路由次数时直接走免费链；跨 UTC 日重置；崩溃（预留未释放）时偏保守。
- `/health` 文案不含密钥。

---

## 2. 阶段 2A：反馈机制（T2–T4）

### 2.1 目标

用户在手机 Discord 上对每条推送**一次点击**给出判断：🆕 新知、👌 已知、🚫 不关心（📥 存收件箱保留）。只认所有者；每条素材只保留一条有效反馈（可以改，也可以撤销）；数据持久化到 SQLite，供回流选编（§3）和周报（§5.6）使用。

### 2.2 交互设计：为什么是“按钮为主、反应为辅”

**关键限制**：Discord 的反应是挂在**整条消息**上的。现有卡片大多是“多条合一”：`following` 和视野拾遗每条消息最多 5 条，综合新闻约 20 条。在这种消息上点 🆕，无法知道指的是哪一条，所以单靠反应做不到逐条反馈。另外，bot 不预置反应的话，用户在手机上加一个反应要三次点击（长按 → 选表情 → 找表情）。

**方案**：

1. **个人推送（`following`、追踪命中）带逐条按钮**：每条一行，四个按钮 `1🆕 1👌 1🚫 1📥`。消息内条目数 ≤ 5（Discord 一条消息最多 5 行组件），所以最多 20 个按钮。一次点击即生效；点完后消息原地更新，被选中的按钮变绿，便于确认，再点一次同一个按钮就是撤销。按钮用 `discord.ui.DynamicItem` 实现，`custom_id` 模板为 `fb:(?P<k>[nw]):(?P<ref>\d+):(?P<i>\d):(?P<v>new|known|skip|save)`，其中 `n` 表示新闻 run，`w` 表示追踪投递。启动时调用 `bot.add_dynamic_items(...)` 注册，重启后旧消息上的按钮仍然可用（`discord-py==2.7.1` 已支持）。非所有者点击时私密回复“仅所有者可用”，不写任何数据。
2. **任何 bot 消息上的反应（所有者手动加）作为辅助**，共享频道也包括在内。🆕/👌/🚫 先按消息 ID 反查条目：消息只含 1 条时（收件箱卡片、单条追踪）是**精确反馈**；含多条时是**粗反馈**，平均分摊给消息内每一条，每条权重为 1/n，并标记 `coarse=1`。共享频道里 bot 不预置反应，也不做任何回应，所以共享内容一字不改；用户自己加的反应属于用户自己的动作。移除反应即撤销这条反应带来的反馈。
3. 🗑️（收件箱的“丢弃”）和 🚫（不关心）是两个不同含义，互不影响。现有收件箱的 📥 ✅ 🗑️ 反应流程不变。

是否改用“每条一条消息 + 预置反应”的方案见 §9 Q2：那样纯靠反应也能逐条反馈，但每期会多 5 条消息，而且要改共享的流水线发送逻辑，所以不推荐。

### 2.3 数据模型：`<STATE_ROOT>/data/feedback.sqlite3`

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version='1'；exposure_cursor（news deliveries.delivered_at）；tracking_since

CREATE TABLE items (                 -- 被推送过的素材快照（素材池 7 天就清，这里长期保留）
  key TEXT PRIMARY KEY,              -- sha256(canonical_url)[:32]，跨订阅同一条素材同一个 key
  canonical_url TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL,               -- 原始标题
  title_zh TEXT,                     -- 选编给出的中文标题（如有）
  source TEXT NOT NULL,              -- FeedSource.name
  category TEXT NOT NULL,            -- FeedSource.category
  board TEXT NOT NULL,               -- ottawa|sudbury|invest|ai_tech|general，见 2.4
  tags TEXT NOT NULL DEFAULT '[]',   -- 主题标签（来自 following 选编输出，否则为确定性实体，见 3.3）
  excerpt TEXT NOT NULL,             -- 原始 RSS 内容前 300 字
  first_shown_at REAL NOT NULL, last_shown_at REAL NOT NULL, shown_count INTEGER NOT NULL);

CREATE TABLE exposures (             -- 曝光 = 分母；未反馈的曝光 = “没表态”
  id INTEGER PRIMARY KEY, key TEXT NOT NULL,
  ref_kind TEXT NOT NULL,            -- run | watch | inbox
  ref_id TEXT NOT NULL,              -- runs.id / deliveries.id / inbox item id
  subscription TEXT, personal INTEGER NOT NULL,
  channel_id TEXT, message_id TEXT, position INTEGER, item_count INTEGER NOT NULL,
  shown_at REAL NOT NULL, UNIQUE(key, ref_kind, ref_id));
CREATE INDEX exposures_msg ON exposures(message_id);
CREATE INDEX exposures_time ON exposures(shown_at);

CREATE TABLE feedback (              -- 每条素材只有一条有效反馈
  key TEXT PRIMARY KEY,
  verdict TEXT NOT NULL CHECK(verdict IN ('new','known','skip')),
  weight REAL NOT NULL,              -- 精确 1.0；粗反馈 1/n
  coarse INTEGER NOT NULL,
  via TEXT NOT NULL,                 -- button | reaction
  message_id TEXT, emoji TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE feedback_log (          -- 审计/撤销/趋势；verdict 为 NULL 表示撤销
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, verdict TEXT, weight REAL,
  via TEXT NOT NULL, message_id TEXT, at REAL NOT NULL);

CREATE TABLE saves (key TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, at REAL NOT NULL);  -- 📥 按钮或收件箱同 URL
```

**“每条记一次”的规则**：同一个 key 只有一行有效反馈。精确反馈覆盖粗反馈，粗反馈永远不覆盖精确反馈；新的精确反馈覆盖旧的；同一按钮再点一次就删除这一行（撤销）；每次变化都写一条 `feedback_log`。

**保留**：items、exposures、feedback 保留 730 天；feedback_log 保留 365 天。数据量很小（每天几十条曝光）。

### 2.4 板块（board）

用户的板块是 Ottawa、Sudbury、投资、AI 和科技、general，对应 id 为 `ottawa | sudbury | invest | ai_tech | general`。

- 个人信源：**由第一阶段在 `personal_sources.json` 里给每个源指定板块**，建议直接用 `FeedSource.category` 存板块 id。需要和第一阶段确认，见 §7。
- 共享信源（只用于统计，不影响共享选编）：`Finance` → `invest`；`Tech`、`AI` → `ai_tech`；其余（`World`、`Canada`、`Science`、`Video` 等）→ `general`。
- 映射函数 `board_of(source, category)` 放在 `core/feedback/boards.py`，是纯函数。

### 2.5 曝光从哪里来（不改新闻流水线的写入）

- 新闻：`news.sqlite3` 的 `deliveries` 表会永久保留每次投递（字段 subscription、url、payload、run_id、delivered_at）。`payload` 里有 `_article_id`、`_evidence`（原始标题、内容、来源、类别）和选编字段。反馈 Cog 每 10 分钟按 `delivered_at` 游标只读同步一次，写入 `items` 和 `exposures`。是否个人推送由 `TOPICS[subscription.topic].personal` 判断；找不到订阅时按共享处理。
- 消息到条目的映射：新闻用 `runs.message_id → runs.payload`（按顺序就是渲染顺序），追踪用 `watch.sqlite3` 的 `deliveries`，收件箱卡片用 `InboxStore.by_card()`。都是只读查询。
- 只读访问统一由 `core/news/reader.py` 提供：`PoolReader`（`since`、`window`）加 `DeliveryReader`（`deliveries_since`、`run_by_message`、`run_items`），文件不存在时返回空。

### 2.6 模块与命令

| 文件 | 职责 |
|---|---|
| `core/news/reader.py`（新） | 只读访问 `news.sqlite3`（素材、投递、run） |
| `core/feedback/boards.py`（新） | 板块映射 |
| `core/feedback/store.py`（新） | `FeedbackStore`：schema、`record(key, verdict, via, weight, coarse, message_id, emoji)`、`undo()`、`sync_exposures(reader, subscriptions)`、`items_for_message()`、`stats(since)`、`cleanup()` |
| `core/feedback/entities.py`（新） | 确定性实体抽取：英文标题里连续 1–4 个首字母大写词，去掉句首词和停用词；中文标题保留书名号和引号里的内容；每条最多 3 个 |
| `cogs/feedback.py`（新） | `FeedbackButton(DynamicItem)`、`view_for(kind, ref, count, state)`、反应监听（`on_raw_reaction_add/remove`，只处理 🆕👌🚫，只认所有者）、曝光同步 loop（10 分钟）、`/feedback_stats` |
| `cogs/inbox.py`（改） | 把 `_save` 拆出公开方法 `save_payload(payload, *, origin, fallback, source=None, via=None)`，供 📥 按钮调用；原有反应流程不变 |
| `core/inbox.py`（改） | 条目可选字段 `source`、`via`（📥 按钮存的条目能精确归因到信源） |

**`/feedback_stats`**（所有者，私密）：参数 `days?`（7–365，默认 30）、`by?`（Choice：来源 / 板块 / 标签）。输出前 15 行，每行包含曝光数、已评数、🆕/👌/🚫 数、新知率、当前权重（§3.2），最后附上当前发给模型的画像片段（透明可查）。

### 2.7 失败与降级

| 情况 | 处理 |
|---|---|
| 按钮回调找不到条目（run 已被人工 skip 等） | 私密回复“这条已无法反馈”，不写数据 |
| 编辑消息失败（权限/已删除） | 反馈照常写入，只记 warning |
| `feedback.sqlite3` 写失败 | 私密回复失败；不重试，不影响推送 |
| 反馈 Cog 未加载 | 个人推送照常发出，只是不带按钮（§2.8） |

### 2.8 个人推送挂按钮（T4）

- `NewsPipeline.publish(..., view_factory=None)`：在 `deliver` 里，**写完发送意图之后**，如果 `view_factory` 不为空，就调用 `view = view_factory(run_id, len(edition.selected))`，然后 `channel.send(embeds=edition.embeds, view=view)`。`view_factory` 为空时，调用参数和现在**完全一样**（不传 `view`），所以共享订阅的发送调用不变，现有测试也不受影响。
- `cogs/news.py` 只对 `TOPICS[sub.topic].personal` 为真的订阅传入 `view_factory`（从 `bot.get_cog('Feedback')` 取，取不到就传 None），手动发布和定时发布都一样。
- `FollowingTopic` 加 `personal = True`；渲染时给每条加序号 `①…⑤`，与按钮编号对应。这只是渲染改动，不改选编规则，也不升 `version`。
- 按钮视图的构造失败不能影响发送：`view_factory` 抛异常时，记 warning 并以 `view=None` 继续发送，仍然只发一次。

### 2.9 测试（离线）

- `tests/test_feedback_store.py`：精确覆盖粗、粗不覆盖精确、同按钮撤销、改判、日志；曝光同步的游标增量和个人/共享判定（用真实 `NewsStore` 播种 runs/deliveries）；消息到条目的映射；`stats()` 加权计数；板块映射；实体抽取。
- `tests/test_feedback_cog.py`：非所有者点击按钮或加反应都无副作用；按钮 `custom_id` 解析（含非法值）；点击后 `edit_message` 的视图状态；多条消息上的反应按 1/n 记粗反馈；移除反应即撤销；📥 按钮调用 `save_payload` 并写 `saves`。
- `tests/test_news_pipeline.py` 补充：`view_factory=None` 时 `send` 的参数与现在一致；带 factory 时只发一次、意图先于发送；factory 抛异常时以无按钮方式仍只发一次。
- `tests/test_extensions.py`：patch `cogs.feedback.STATE_ROOT`，并确认 `SCHEDULED_JOBS_ENABLED=False` 时同步 loop 不启动。

---

## 3. 阶段 2B：反馈回流选编（T5–T6）

### 3.1 作用范围

- **只改个人线路**：`following` 订阅（专题 `FollowingTopic`）。第一阶段如果把个人订阅拆成按板块的多份订阅，每份都适用同一套逻辑。追踪确认（§4）也会拿到画像里的 `known_topics`，用来在命中上标注“可能已知”。
- 共享专题完全不读画像。共享频道里的反馈只进统计和周报（用于判断是否砍信源），不改共享选编。

### 3.2 统计与权重（`core/feedback/profile.py`，纯函数，零模型）

窗口取最近 90 天的有效反馈，时间衰减半衰期 30 天：d = 0.5^(age_days / 30)。每条反馈的贡献为 `weight × d`，其中精确反馈的 weight 为 1，粗反馈为 1/n。

- 全局基线：p0 = (Σnew + 1) / (Σnew + Σknown + 2)；q0 = (Σskip + 1) / (Σnew + Σknown + Σskip + 2)。
- 对每个特征值 f（某个来源 / 某个板块 / 某个标签），记 n_f、k_f、s_f 为 new、known、skip 的加权和：
  - 新知率（Beta 平滑，先验强度 4）：r_f = (n_f + 4·p0) / (n_f + k_f + 4)
  - 不关心率：z_f = (s_f + 4·q0) / (n_f + k_f + s_f + 4)
  - 权重：W_f = clamp( (r_f / p0) × ((1 − z_f) / (1 − q0)), 0.25, 4.0 )
- **冷启动**：90 天内精确反馈不足 15 条时，画像为空（返回 None），选编与现在完全相同。单个来源的有效评价（n+k+s）不足 5 时，W 取 1。

### 3.3 主题标签从哪来（不额外调模型）

- `FollowingTopic` 的输出 schema 每条增加 `tags`：1–3 个中文短语，专有名词保留原文（如 `Nvidia`、`OPG`），每个 ≤12 字。选编时同时把当前高频标签词表（≤40 个）放进输入，要求“同一主题尽量复用词表里的标签”，以免同义标签越来越多。标签由已有的那次选编调用产出，**不增加调用次数**。
- 标签随投递一起存进 `runs.payload` 和 `deliveries.payload`，曝光同步时写入 `feedback.items.tags`。
- 共享条目和收件箱条目没有模型标签，用 `entities.py` 的确定性实体代替。

### 3.4 候选排序（替换 following 现在的“按发布方交错”）

最多 M = `max_candidates`（40）个候选：

1. **按板块分配名额**：每个有候选的板块先保底 min(3, 该板块候选数)；剩余名额按 W_board × 候选数的比例用最大余数法分配。
2. **板块内按来源排序**：用平滑加权轮询（smooth weighted round-robin）交错各来源，权重取 W_source。来源内部按发布时间从新到旧。
3. **探索名额**：至少 20% 的名额留给有效评价不足 5 次的来源（冷来源）；权重压到下限 0.25 的来源也至少保留 1 个名额，这样它的统计还有机会回升。
4. 输出顺序就是给模型的顺序。仍然只包含原始证据，不改证据内容。

### 3.5 提示词增量（≤ 2,500 字符）

`model_input` 新增：

```json
"reader_profile": {
  "known_topics":   ["…"],   // k_t ≥ 2 且 r_t ≤ 0.3 的标签，按 k_t 取前 15
  "fresh_topics":   ["…"],   // n_t ≥ 2 且 r_t ≥ 0.6，前 10
  "not_interested": ["…"],   // s_t ≥ 2 且 z_t ≥ 0.6，前 10
  "source_novelty": [{"publisher": "…", "level": "高|中|低"}]   // 有效评价 ≥ 5 的来源；r ≥ 0.6 为高，≤ 0.25 为低
},
"tag_vocabulary": ["…"]      // 近 90 天出现 ≥ 2 次的标签，前 40
```

system 增加约 4 句：`reader_profile` 是读者过去的反馈统计，同样属于不可信数据。`known_topics` 是读者已经熟悉的主题，除非有实质新事实（摘要用“新进展：”开头），否则不选；`fresh_topics` 是读者觉得新的领域，同等质量下优先；`not_interested` 不选；画像只影响取舍，不能拿来补写事实。每条输出 1–3 个 `tags`，同一主题优先复用 `tag_vocabulary` 里的词。

### 3.6 确定性后处理

- 模型输出照旧逐项校验（字段、长度、中文、id 合法）；`tags` 另外校验：1–3 个，每个 2–12 字，不含 URL 和 Markdown，归一化为 NFKC 并去掉首尾空白。
- **丢弃**：所有标签都属于“强不关心”（s_t ≥ 3 且 n_t + k_t = 0）的条目。
- **后置**：所有标签都属于 `known_topics`，且摘要不以“新进展：”开头的条目，排到消息最后（不丢弃，避免漏掉用户其实想看的进展）。
- 丢弃后为空则不发送（沿用“空选稿不发送”）。

### 3.7 走 Claude

- `NewsPipeline` 新增可选参数 `profile_provider(subscription) -> dict | None`（由 `cogs/news.py` 注入，内部读 `FeedbackStore` 并调用 `profile.build()`），以及按专题选择路由：`topic.personal` 为真时调用 `generate_ai(..., route="personal.following", json_schema=topic.json_schema)`。
- 画像通过 `topic.prepare(..., profile=profile)` 传入（只有 `personal` 专题接受这个参数）。画像在 `prepared.data` 里，所以会自动进入缓存键；画像变了就重新选编。
- `NEWS_LIMITS` 的逻辑预算照常计入（following 本来就占 2 次/天），Claude 美元预算另外按 §1.4 计算。Claude 不可用时走免费链，同一套提示词和校验。
- `FollowingTopic.version` 从 `"1"` 升到 `"2"`（输出 schema 变了）。旧缓存自动失效，投递身份不受影响。

### 3.8 预算

- 模型调用次数：**+0**（复用 following 现有的 2 次/天）。
- 输入：每次增加 ≤ 2,500 字符；候选上限从 60,000 字符收紧到 30,000 字符（§1.4 的路由上限），每条 RSS 证据仍 ≤ 600 字。
- 输出：每条多 1–3 个标签，约多 150 token，在 Claude 的 `max_tokens` 6,000 和免费链的 3,000 范围内。

### 3.9 怎么验证“越来越偏向新知”

周报（§5.6）给出个人推送最近 4 周的新知率趋势 new / (new + known)，以及“未表态率”。`/feedback_stats` 能看到当前权重和画像。如果 4 周后新知率没有上升，再调整先验强度或权重上下限，而不是加模型调用。

### 3.10 测试（离线）

- `tests/test_feedback_profile.py`：衰减、平滑、clamp、冷启动阈值、粗反馈权重；标签集合的阈值与排序；画像长度上限。
- `tests/test_following_feedback.py`：板块保底和最大余数分配；平滑加权轮询的比例；探索名额；画像为 None 时候选顺序与旧算法一致；`tags` 校验；强不关心丢弃、已知后置；`route` 和 `json_schema` 只在 personal 专题上传给 `generate_ai`（共享专题的 mock 调用参数不变）；画像变化导致缓存键变化。

---

## 4. 阶段 2C：主题追踪（可选第二步，T7–T11）

v1 设计整体保留，以下是调整：

- **定位**：作为板块下的细化，例如 `invest` 板块下追踪“加拿大利率”，`ai_tech` 板块下追踪“固态电池”。主题定义新增 `board` 字段（可选，为空表示全部板块），只匹配该板块的素材（按 `board_of()`）。
- **推送带反馈按钮**：每条消息 ≤ 5 条命中（受 5 行组件限制；原来是 10 条），按钮与 §2.2 相同（`fb:w:<delivery_id>:<i>:<v>`）。追踪命中的反馈同样进入画像和周报。
- **确认走 Claude**：`watch.confirm`（effort low），不可用时走免费链。追踪自己的 `WATCH_LIMITS` 预算继续保留，作为逻辑调用上限；美元另由 `CLAUDE_LIMITS` 约束。
- **可能已知标注**：如果命中条目的实体或模型 note 与画像 `known_topics` 有重叠，就在条目后标“（可能已知）”，不丢弃。

### 4.1 数据模型

**定义**：`<STATE_ROOT>/watches.json`（`JsonStore`）

```json
{"version": 1, "watches": {"w-3f9a1c": {
  "id": "w-3f9a1c", "name": "固态电池", "board": "ai_tech",
  "keywords": ["固态电池", "solid-state battery", "QuantumScape"], "exclude": ["游戏"],
  "description": "关注量产、装车和关键材料突破，不要股评。", "mode": "confirm",
  "paused": false, "resume_at": null, "created_at": 0, "updated_at": 0, "removed_at": null}}}
```

上限：活跃主题 ≤ 20 个，总数 ≤ 50；关键词 1–12 个（OR 关系），排除词 0–8 个，每个 2–40 字；描述 ≤ 200 字。软删除 180 天后物理清除定义，命中历史保留。

**状态**：`<STATE_ROOT>/data/watch.sqlite3`

```sql
meta(key, value)   -- schema_version, pool_cursor, known_sources, last_cleanup
hits(id PK, watch_id, article_id, url, canonical_url, title, source, category, board,
     published_at, first_seen, excerpt, terms JSON, score, status, note, attempts,
     delivery_id, created_at, updated_at, UNIQUE(watch_id, article_id))
deliveries(id PK, watch_id, watch_name, channel_id, status, hit_ids JSON, message_id, created_at, updated_at)
budgets(day PK, calls, output_tokens, messages)
```

命中状态：`pending → ready | rejected | ready_unconfirmed | unconfirmed`；`ready* → sending → delivered | uncertain`；另有 `folded`（超过每条 5 个）、`capped`（超过当日消息上限）、`stale`（发布时间超过 36 小时）、`seeded`（新信源的首批，避免第一阶段新增信源时把 72 小时的存量一次推出）、`dup`（同主题已有同 canonical_url，或 72 小时内同标题）。保留：hits 365 天且 ≤ 20,000 行；deliveries 365 天；budgets 14 天。

### 4.2 模块

`core/watch/{models,definitions,matcher,store,scanner,confirm,render,delivery}.py` 加 `cogs/watch.py`，职责与 v1 相同：

- matcher：ASCII 词用 `(?<![A-Za-z0-9])term(?![A-Za-z0-9])` 并忽略大小写；含中日韩文字的词做 casefold 子串匹配；排除词优先；标题命中计 ×3。
- scanner：通过只读 `PoolReader.since(pool_cursor)` 读取新素材。同一批采集在一个事务里提交、共用同一个 `now`，所以不会读到半批。命中写入和游标推进放在同一个事务里。
- 确认：每个 tick 至多一次批量调用；每个主题 ≤ 8 条、总计 ≤ 24 条、≤ 12,000 字符，不传 URL；输出 `{"confirmed":[{"id","note"}]}`（Claude 用 json_schema），note ≤ 40 字中文、不含 URL。连续两次失败、预算用尽或等待超过 6 小时就降级：标题命中，或正文命中 ≥ 2 个不同关键词的，照推并标“未经模型确认”，其余只进历史。
- 投递：每个主题一把锁，使用 `run_delivery_job`（build 不重试）。先写意图（检查日消息上限、记 `sending`），再 `send` 一次，成功后 `delivered`；异常或重启都标 `uncertain`，永不补发，也不阻塞后续命中。频道优先 `WATCH_CHANNEL_ID`，否则用 `INBOX_CHANNEL_ID`；两者都没有时不发、不计数。

### 4.3 命令（所有者专用，私密）

| 命令 | 参数 |
|---|---|
| `/watch_add` | `name`、`keywords`、`board?`（Choice）、`exclude?`、`description?`、`mode?`、`expand?: bool`（调一次 `watch.aliases` 补中英别名并列在回复里） |
| `/watch_edit` | `watch`（自动补全），其余参数可选 |
| `/watch_remove` | `watch` |
| `/watch_pause` | `watch`、`paused: bool=true`（恢复后不补报暂停期间的素材，靠 `resume_at`） |
| `/watch_list` | — |
| `/watch_hits` | `watch`、`days?=7`、`status?` |
| `/watch_test` | `watch`：对最近 7 天素材做关键词试跑，私密列出前 15 条，不调模型，不写库 |

### 4.4 调度与错峰

- `watch_tick` 每 10 分钟一次，single-flight，`SCHEDULED_JOBS_ENABLED=false` 时不启动。顺序：scan → confirm → deliver → 每日 cleanup。
- 模型错峰：落在任一定时生成时刻 `[T−2min, T+8min]` 窗口内的 tick 不调模型，只做扫描和纯关键词投递。窗口来自 `load_subscriptions()` 里启用订阅的时刻（默认 08:00、08:20、08:45、12:00、15:30、18:00、18:20），再加 07:30（英文阅读）和 08:15（AI 日报）。

### 4.5 预算

| 项 | 默认 |
|---|---|
| `WATCH_LIMITS.daily_calls` | 16（其中 Claude 至多 6 次确认 + 3 次别名建议，其余走免费链） |
| `WATCH_LIMITS.daily_output_tokens` | 24,000（免费链每次确认 1,500） |
| `WATCH_LIMITS.daily_messages` | 24 |

### 4.6 测试

与 v1 相同：matcher 边界、scanner 游标/stale/seeded/dup/暂停、confirm 校验/降级/错峰、delivery 只发一次/不确定/folded/capped、命令只认所有者、`/watch_test` 不写库。另外补充：板块过滤、≤ 5 条加按钮、Claude 路由参数。

---

## 5. 阶段 3：知识回环（T12–T16）

### 5.1 目标

1. 素材池出现过的条目和收件箱里的 Markdown 进入一个**长期、有上限**的本地索引。
2. `/recall <问题>`：先在本地检索取证据，再让模型按 `[S1]` 编号作答，最后由程序附上来源链接（复用 `core/web_search.py` 的 `SearchSource` 和 `format_grounded_answer`，即 arch.md 7.1 的模式）。
3. 每周回看报告（纯统计，默认不调模型），回答：本周多少新知、多少已知；哪些来源产出新知最多；哪些来源一条新知都没有；另外包括存了什么、读完了什么、追踪命中了什么。

### 5.2 数据模型：`<STATE_ROOT>/data/knowledge.sqlite3`

建表前先设 `PRAGMA auto_vacuum=INCREMENTAL`，每次清理后执行 `incremental_vacuum(2000)`。

```sql
meta(key, value)   -- schema_version, news_cursor, inbox_fingerprint, fts5, tracking_since
docs(rowid INTEGER PK, id TEXT UNIQUE,               -- 'news:<article_id>' | 'inbox:<item_id>'
     kind TEXT CHECK(kind IN ('news','inbox')), source, category, board,
     url, canonical_url, title, body,                  -- 新闻：RSS 原文 ≤ 4,000 字；收件箱：去掉头部的 Markdown ≤ 50,000 字
     published_at, added_at, state, state_at, pinned INTEGER, keep_until, version)
  -- 索引：canonical_url；(kind, added_at)；source
CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tokenize='unicode61 remove_diacritics 2');
  -- rowid 与 docs.rowid 对齐，存 index_form() 处理后的文本（中日韩文字转为重叠双字）
budgets(day PK, calls, output_tokens)
reports(week PK, status, channel_id, message_id, payload, created_at, updated_at)
```

用普通 FTS5 表，而不用 external-content 或 contentless 表。原因：生产环境的 SQLite 3.40 没有 `contentless_delete`（3.43 才有）；external-content 要求索引文本就是原文，而这里索引的是双字切分后的文本。代价是多占一倍空间，换来增删简单可靠：同一事务里先 `DELETE FROM docs_fts WHERE rowid=?` 再插入。

**保留**：

| 类别 | 保留期 | 上限 |
|---|---|---|
| 新闻（未 pin） | 90 天 | 共 60,000 条 |
| 新闻（pinned：被个人推送过、有反馈、被收藏） | 365 天 | 计入 60,000 条，最后才删 |
| 收件箱 pending/done | 不过期 | 5,000 条（超出只告警） |
| 收件箱 dropped | `state_at` 后 30 天移出索引（Markdown 文件不动） | — |
| budgets / reports | 14 天 / 104 周 | — |

估算总大小约 150–200 MB。每天 03:40 清理一次。

### 5.3 模块

| 文件 | 职责 |
|---|---|
| `core/knowledge/text.py` | `index_form`（NFKC + casefold，中日韩文字转双字）、`query_terms`、`passage`（取 ≤ 600 字的窗口）、`fts_query`（每个词作为短语并转义，防止 FTS 语法注入） |
| `core/knowledge/store.py` | schema、FTS5 自检（失败则 `fts5='0'`，`search()` 抛 `KnowledgeUnavailable`）、upsert/delete（与 FTS 行同一事务）、`search()`（`bm25(docs_fts, 3.0, 1.0)`）、`pin_keys()`、`cleanup()`、预算、周报 claim/intent/complete；连接使用 `check_same_thread=False` 加锁，配合 `asyncio.to_thread` |
| `core/knowledge/sync.py` | `sync_news`（PoolReader 游标）、`sync_inbox`（`index.json` 指纹不变就跳过）、`sync_pins`（反馈库里有曝光或反馈的 key，加上收件箱 canonical_url） |
| `core/knowledge/recall.py` | 检索词规划、回退检索词、检索与多样化、证据转 `SearchSource`、作答与格式化 |
| `core/knowledge/review.py` | 纯函数：统计 → 渲染 ≤ 2 个 embed（≤ 5,800 字符） |
| `core/inbox.py`（改） | 状态变化写 `state_at`；新增 `items()` |
| `cogs/knowledge.py` | `/recall`、`/review`、同步 loop（30 分钟）、维护（03:40）、周报（20:30，仅周日执行） |

### 5.4 命令

| 命令 | 参数 | 可见性 |
|---|---|---|
| `/recall` | `question`（≤ 300 字）、`scope?`（全部/收件箱/新闻）、`days?`（7–365，默认 90） | 所有者；冷却 30 秒。在个人频道里调用时普通回复；在其他任何频道里调用时 `ephemeral` |
| `/review` | `week?`（this/last） | 所有者；私密预览，不占用定时周报的身份 |

### 5.5 /recall 流程与预算

1. 检查所有者 → `defer`。
2. **检索词规划**（素材池以英文为主，用户用中文提问）：`generate_ai(route="recall.plan", json_schema={"zh":[str],"en":[str]}, json_mode=True, max_output_tokens=200)`。本地校验每类 ≤ 4 个、每个 ≤ 40 字符。失败时用 `fallback_terms()`：问题里的 ASCII 词（去停用词），加上中文双字（去掉“什么”“一下”“这个”这类常见虚词）。
3. **检索**：取 bm25 前 40 → 同一来源最多 3 条 → 收件箱条目分数 ×1.5 → 取前 12 条 → 每条 `passage()` ≤ 600 字，证据合计 ≤ 8,000 字符。**没有结果就直接回复“本地没有相关记录”，不调用作答模型**（作答额度是在检索有结果后才预留的）。
4. **作答**：`generate_ai(prompt, system=RECALL_SYSTEM, route="recall.answer", max_output_tokens=1200)`，外层 `asyncio.timeout(90)`。提示词沿用 `build_grounded_prompt` 的约束，并说明材料来自用户自己的收藏和新闻存档，材料不足时直说，不能凭记忆补“最新”事实。
5. **呈现**：`format_grounded_answer()` 由程序附链接（≤ 6 条），再经 `create_ai_embed()`。收件箱条目没有原始 URL 时，用保存时的消息跳转链接。作答模型全部失败时，零模型兜底：列出前 6 条命中的标题和链接。
6. **免费链侧的预算** `RECALL_LIMITS`：`daily_calls` 40，`daily_output_tokens` 28,000。Claude 侧由 §1.4 约束（规划和作答各 15 次/天）。

### 5.6 周报

- **时间**：每周日 20:30（`BOT_TIMEZONE`），与现有定时任务都不冲突。停机错过不补发。按 ISO 周 claim，at-most-once 语义：`sending` → `delivered`，出错为 `uncertain`，同一周不重发。
- **内容**（全部是确定性统计）：
  1. **新知与已知**：本周个人推送的曝光数；🆕 / 👌 / 🚫 / 未表态 各多少（精确与粗反馈分开列，粗反馈按 1/n 加权）；本周新知率，以及最近 4 周的新知率趋势。
  2. **新知最多的来源**：按本周 🆕 加权数排序取前 5，同时列新知率（有效评价 ≥ 3 才列率）。
  3. **一条新知都没有的来源**：本周有曝光但 🆕 为 0 的来源。另列**砍源候选**，条件为最近 28 天曝光 ≥ 10、有效评价 ≥ 5、🆕 = 0；以及最近 28 天曝光为 0 的来源（从没被选上）、本周入池 0 条的来源（疑似死源，参考 WSJ Markets 那次）。只给建议，不自动改配置。
  4. **按板块**：五个板块各自的曝光、🆕、👌、🚫。
  5. **收藏与阅读**：本周存入数和前 10 条标题（及其来源归因）；本周读完数；当前未读总数，以及最老一条未读放了多少天。
  6. **追踪**（如果已启用）：每个主题的命中、已推送、被否决、不确定数。
  7. **知识库规模**：文档数、库文件大小；本周 Claude 花费和本月累计花费。
- **信源清单**来自第一阶段完成后的“共享组 + `personal_sources.json`”，需要第一阶段提供一个只读的 `all_sources()`（§7）。
- 默认不调用模型。可选的“本周小结”走 `review.summary`（§9 Q7）。

### 5.7 调度

| 任务 | 时间 |
|---|---|
| 知识库同步 | 每 30 分钟（不调模型，不需要错峰） |
| 维护 | 每天 03:40（知识库清理和 vacuum；追踪、反馈各自的清理放在它们自己的 loop 里，幂等） |
| 周报 | 每天 20:30 触发，只在周日执行 |
| `/recall` | 交互触发，`Semaphore(1)` |

### 5.8 失败与降级

| 情况 | 处理 |
|---|---|
| FTS5 不可用 | `/recall` 回复“检索不可用”；同步照样写 `docs`；`/health` 和 healthcheck 报 warning，但**不作为 strict 失败**，避免整次部署被回滚 |
| 规划失败 | 用回退检索词 |
| 作答失败 | 零模型兜底（列出命中标题和链接） |
| 收件箱文件缺失 | 跳过该条并 warning |
| 周报构建异常 / 发送异常 | 构建异常记 `failed`，发送异常记 `uncertain`，本周都不重发 |

### 5.9 Embedding（不在本阶段做）

FTS5 加上双语检索词规划，已经能覆盖“中文问、英文素材”的主要情况。如果以后召回不够，有两条不加容器的路：

- **进程内 ONNX 小模型**，例如量化版 multilingual-e5-small：模型 100 多 MB，常驻内存约 200–400 MB（compose 的 `mem_limit` 可能要从 768m 调到 1g），镜像也会变大；6 万条 × 384 维约 90 MB，numpy 暴力算余弦一次约 10 ms。
- **免费 embedding API**：会把私人收藏内容发给第三方。

以上数字都是估算，实施前要实测。建议上线两周后看 `/recall` 的零结果率再决定（§9 Q5）。

### 5.10 测试

- `test_knowledge_store.py`：双字切分、FTS 转义、upsert/delete 与 FTS 同步、保留策略、FTS5 不可用分支。
- `test_knowledge_sync.py`：新闻游标、收件箱增量和 `state_at`、pin。
- `test_recall.py`：规划失败回退、无结果不作答、证据上限、同来源 ≤ 3、引用编号转链接、作答失败兜底、预算、`route` 参数。
- `test_review.py`：新知/已知统计、粗反馈加权、砍源候选规则、趋势、板块、归因覆盖率、渲染长度。
- `test_knowledge_cog.py`：只认所有者、非个人频道 `ephemeral`、周报 at-most-once、非周日不执行、扩展加载。

---

## 6. 模型预算总表（每天）

| 功能 | 走 Claude 的条件 | Claude 次数上限 | 免费链逻辑上限 | 备注 |
|---|---|---|---|---|
| 共享专题（general/discovery/power） | 永不 | 0 | `NEWS_LIMITS`：24 次 / 72,000 token（不变） | 一字不改 |
| following 选编（含画像） | `personal.following` | 4 | 计入 `NEWS_LIMITS` | 每天 2 次定时 |
| 反馈按钮/反应、画像计算 | — | 0 | 0 | 纯 SQL 和算术 |
| 追踪确认 / 别名 | `watch.confirm` / `watch.aliases` | 6 / 3 | `WATCH_LIMITS`：16 次 / 24,000 token | 可选功能 |
| /recall 规划 / 作答 | `recall.plan` / `recall.answer` | 15 / 15 | `RECALL_LIMITS`：40 次 / 28,000 token | 无结果不作答 |
| 周报 | `review.summary`（默认关闭） | 1/周 | 0 | 默认纯统计 |
| **Claude 美元上限** | | | | **$0.60/天，$19/月；典型约 $0.43/天（约 $13/月）** |

全 bot 同时进行的生成调用至多 4 个：新闻 2、追踪 1、/recall 1。

---

## 7. 与第一阶段及现有代码的冲突点、需要改动的现有文件

### 7.1 与第一阶段（个人信源配置化）的冲突与约定

| 文件或约定 | 风险 | 处理 |
|---|---|---|
| `core/news/sources.py` | 第一阶段改为读 `personal_sources.json` | 本设计不改它；**需要第一阶段提供**：每个个人源有板块（建议写在 `FeedSource.category` 里，取值为板块 id），并提供只读的 `all_sources()`，能区分共享和个人。如果第一阶段没提供，T15 在第一阶段合并后的代码上补 |
| `core/news/topics/discovery.py`（`FollowingTopic`） | 第一阶段如果按板块拆专题或改 following | **高冲突**：T4、T6 都改这里，必须等第一阶段合并后再开工 |
| `core/news/pipeline.py`、`cogs/news.py` | 第一阶段可能在 news cog 里加信源命令 | T4、T6 改这两处（`view_factory`、`profile_provider`、`route`），等第一阶段合并后开工，改动尽量小 |
| `core/news/subscriptions.py` | following 订阅可能变动 | 只读调用 |
| `core/news/ingest.py`、`store.py` | `articles` 表的列如果变了 | `reader.py` 要同步跟进；本设计不改这两个文件 |
| 信源名 | 历史统计以 `FeedSource.name` 为键 | 约定 name 是稳定 ID，改名等于删旧源、加新源（§9 Q11） |
| `tests/test_extensions.py`、`cogs/health.py`、`arch.md`、`docs/` | 两边都会改 | 合并时手工处理；本设计的文档写进新文件 `docs/personal.md`，arch.md 只加新小节 |
| 命令名 | 第一阶段大概会用 `/source_*` | 本设计用 `/feedback_stats`、`/watch_*`、`/recall`、`/review`，不冲突。目前约 42 个命令，加上第一阶段约 4 个和本设计 10 个，远低于 100 个上限 |

### 7.2 需要改动的现有文件

| 文件 | 任务 | 改动 |
|---|---|---|
| `core/ai_client.py` | T1 | Claude provider、`route` 和 `json_schema` 参数、预算、冷却、状态 |
| `requirements.txt`、`requirements.lock`、`.env.example` | T1 | 加 `anthropic`、重建锁文件、加空占位 |
| `cogs/health.py` | T1、T3、T11、T16 | Claude 状态与用量；反馈同步、追踪、知识库的 loop；个人频道 |
| `scripts/healthcheck.py` | T1、T12 | Claude 已配置/未配置；FTS5 检查（warn） |
| `core/news/pipeline.py` | T4、T6 | `view_factory`、`profile_provider`、按专题选 `route`/`json_schema`（共享路径不变） |
| `cogs/news.py` | T4、T6 | 给 personal 订阅注入上面这些参数 |
| `core/news/topics/discovery.py` | T4、T6 | `FollowingTopic`：`personal=True`、序号、画像、标签、schema、`version="2"`（`DiscoveryTopic` 不变） |
| `cogs/inbox.py` | T3 | 公开的 `save_payload()` |
| `core/inbox.py` | T3、T13 | 可选字段 `source`、`via`；`state_at`；`items()` |
| `tests/test_extensions.py`、`tests/test_news_pipeline.py`、`tests/test_inbox.py` | 多个任务 | patch 与回归用例 |
| `arch.md` | T1、T4、T6、T11、T16 | §4 provider 路由与预算、新闻投递按钮、新增小节、存储、配额 |
| **不改** | — | `core/web_search.py`（直接复用）、`core/news/store.py`、`core/news/topics/general.py`、`power_projects.py`、`DiscoveryTopic`、`settings.json`、共享频道设置 |

新增公开设置键（都可选，有默认值）：`CLAUDE_LIMITS`、`WATCH_CHANNEL_ID`、`WATCH_LIMITS`、`RECALL_LIMITS`。新增私密配置只有一个：`ANTHROPIC_API_KEY`。

---

## 8. 任务拆分

约定：每个任务一个 PR，在独立的 git worktree 里做（第一阶段的 agent 在同一个仓库工作）。每个任务都要跑一次最相关的测试，再跑 `git diff --check`。涉及命令接线或共享配置的任务（T1、T3、T4、T6、T11、T16）还要跑 `python scripts/validate.py --allow-missing-secrets`。测试不写真实 `data/`，不调用任何真实模型。

### 前置

**T1 Anthropic provider 与 Claude 预算**
- 文件：改 `core/ai_client.py`；新增 `core/claude_budget.py`（`JsonStore` 用量、预留与结算、成本估算）；改 `cogs/health.py`、`scripts/healthcheck.py`、`requirements.txt`、`requirements.lock`、`.env.example`、`arch.md` §4；新增 `tests/test_claude_provider.py`。
- 验收：§1.2–1.5 全部语义；不传 route 时行为逐字节不变（现有 provider 测试全过）；请求不含 `thinking`，`effort` 按路由设置，JSON 用 `output_config.format`，没有 prefill；按 §1.5 分类降级，额度耗尽当日停用；`daily_usd`、`monthly_usd`、路由次数三重上限生效；`/health` 显示状态、当日和本月用量，不泄露密钥；没有密钥时不注册、不 warning；**`requirements.lock` 用 `uv pip compile requirements.txt --python-version 3.13 --universal --generate-hashes --output-file requirements.lock` 重建，diff 只新增 anthropic 及其依赖（全部带 hash），已有 pin 不变，在干净 venv 里 `--require-hashes` 安装成功**；`validate.py --allow-missing-secrets` 通过。
- 依赖：无。

### 阶段 2A：反馈机制

**T2 反馈存储、只读访问层与曝光同步**
- 文件：新增 `core/news/reader.py`、`core/feedback/__init__.py`、`core/feedback/boards.py`、`core/feedback/entities.py`、`core/feedback/store.py`、`tests/test_feedback_store.py`。
- 验收：§2.3 的 schema 与“每条记一次”规则；`sync_exposures` 的游标增量、个人/共享判定、快照字段；消息到条目的映射（runs、收件箱卡片；watch 部分留接口，没有库时返回空）；`stats()` 加权；板块映射；实体抽取；只读连接不创建文件；清理规则。
- 依赖：无（可与 T1 并行）。

**T3 反馈 Cog：按钮、反应、📥、统计命令**
- 文件：新增 `cogs/feedback.py`、`tests/test_feedback_cog.py`；改 `cogs/inbox.py`（`save_payload`）、`core/inbox.py`（`source`、`via`）、`tests/test_inbox.py`、`tests/test_extensions.py`、`cogs/health.py`。
- 验收：§2.2 交互（DynamicItem 重启后仍可用、只认所有者、选中状态更新、同按钮撤销）；反应的精确/粗反馈和移除撤销；📥 按钮存入收件箱并记录来源；`/feedback_stats`；曝光同步 loop 每 10 分钟，`SCHEDULED_JOBS_ENABLED=false` 时不启动；store 懒加载；`cog_unload` 时移除 dynamic items、关闭数据库；现有收件箱测试全过。
- 依赖：T2。

**T4 个人推送挂按钮**（第一阶段合并后开工）
- 文件：改 `core/news/pipeline.py`（`view_factory`）、`cogs/news.py`、`core/news/topics/discovery.py`（`FollowingTopic.personal`、序号渲染）、`tests/test_news_pipeline.py`、`tests/test_news_commands.py`、`arch.md` §5/§6。
- 验收：§2.8：共享订阅的 `send` 调用参数不变（测试断言）；personal 订阅带按钮，仍然只发一次，意图先于发送；factory 异常时以无按钮方式发送一次；序号与按钮一致；`DiscoveryTopic` 的渲染不变。
- 依赖：T3、第一阶段。

### 阶段 2B：反馈回流

**T5 画像计算**
- 文件：新增 `core/feedback/profile.py`、`tests/test_feedback_profile.py`。
- 验收：§3.2 的公式（衰减、平滑、clamp、冷启动阈值）；§3.5 各标签集合的阈值、排序和长度上限；纯函数，输入是 `FeedbackStore` 的查询结果。
- 依赖：T2。

**T6 following 选编使用画像，并走 Claude**（第一阶段合并后开工）
- 文件：改 `core/news/topics/discovery.py`（`FollowingTopic`：候选排序 §3.4、画像输入 §3.5、`tags` 校验与后处理 §3.6、`json_schema`、`version="2"`）、`core/news/pipeline.py`（`profile_provider`、按 `topic.personal` 选 `route`/`json_schema`）、`cogs/news.py`（注入 provider）、`core/feedback/store.py`（曝光同步时读 `tags`）；新增 `tests/test_following_feedback.py`；改 `arch.md` §6。
- 验收：§3.10 全部；共享专题调用 `generate_ai` 的参数不变；没有画像时候选顺序与旧算法一致；预算（§3.8）可在测试里断言输入上限。
- 依赖：T1、T4、T5。

### 阶段 2C：主题追踪（可选；用户不要可以整段跳过）

**T7 主题定义与匹配器（含板块）**
- 文件：`core/watch/{__init__,models,definitions,matcher}.py`、`tests/test_watch.py`。
- 验收：§4.1 定义的上限和唯一性；关键词分隔解析；板块字段；matcher 边界用例。
- 依赖：T2（`boards.py`）。

**T8 WatchStore 与扫描器**
- 文件：`core/watch/store.py`、`core/watch/scanner.py`、测试。
- 验收：schema、启动时 `sending` 改为 `uncertain`、命中写入与游标推进同一事务、stale/seeded/dup/暂停/`resume_at`/板块过滤、清理。
- 依赖：T7。

**T9 命中确认（Claude 路由加免费链降级）**
- 文件：`core/watch/confirm.py`、测试。
- 验收：输入上限；`route="watch.confirm"` 加 `json_schema`；校验；`WATCH_LIMITS`；错峰窗口；降级分流；`suggest_aliases`（`watch.aliases`）；“可能已知”标注（读画像）。
- 依赖：T8、T1、T5。

**T10 渲染与投递（带反馈按钮）**
- 文件：`core/watch/render.py`、`core/watch/delivery.py`、测试；`core/feedback/store.py` 补上 watch 曝光同步。
- 验收：每条消息 ≤ 5 条命中加按钮（`fb:w:`）；at-most-once；folded/capped；频道回退；追踪曝光进入反馈库。
- 依赖：T8、T3。

**T11 追踪 Cog 与文档**
- 文件：`cogs/watch.py`、`tests/test_watch_cog.py`、`tests/test_extensions.py`、`cogs/health.py`、`arch.md`；新增 `docs/personal.md`（反馈与追踪部分）。
- 验收：§4.3 的 7 个命令；tick 流程；只认所有者；`/watch_test` 不写库；/help 断言通过；`validate.py` 通过。
- 依赖：T9、T10。

### 阶段 3：知识回环

**T12 知识库存储与中文双字索引**
- 文件：`core/knowledge/{__init__,text,store}.py`、`tests/test_knowledge_store.py`、`scripts/healthcheck.py`（FTS5 warn）。
- 验收：§5.2 与 §5.8 中 FTS 相关的部分；FTS 查询转义防注入。
- 依赖：无（排在阶段 2 之后，可提前并行开工）。

**T13 同步（新闻、收件箱、pin）**
- 文件：`core/knowledge/sync.py`、`tests/test_knowledge_sync.py`；改 `core/inbox.py`（`state_at`、`items()`）、`tests/test_inbox.py`。
- 验收：§5.3 sync 各函数；旧条目没有 `state_at` 时不报错；pin 规则（反馈库 key、收件箱 URL）。
- 依赖：T12、T2。

**T14 /recall 核心**
- 文件：`core/knowledge/recall.py`、`tests/test_recall.py`。
- 验收：§5.5；规划和作答分开预留；复用 `web_search`（不改该文件）；`route` 参数正确。
- 依赖：T12、T1。

**T15 周报核心**
- 文件：`core/knowledge/review.py`、`tests/test_review.py`；如有需要，在第一阶段代码上为 `core/news/sources.py` 补 `all_sources()`。
- 验收：§5.6 第 1–7 段的统计全部正确（重点是新知/已知、新知最多和零新知来源、砍源候选规则）；周的边界按 `BOT_TIMEZONE`；没有追踪或收件箱数据时显示“暂无”；渲染上限；纯函数。
- 依赖：T13、T5、第一阶段（T8 可选；没有追踪库时跳过该段）。

**T16 知识 Cog 与文档**
- 文件：`cogs/knowledge.py`、`tests/test_knowledge_cog.py`、`tests/test_extensions.py`、`cogs/health.py`、`arch.md`、`docs/personal.md`（知识回环部分）。
- 验收：§5.4、§5.7；周报 at-most-once；阻塞的 SQLite 操作都放进 `to_thread`；`validate.py` 通过。
- 依赖：T13、T14、T15。

合计 **16 个任务**（其中 T7–T11 可选）。可以并行的：T1 与 T2；T5 与 T3；T9 与 T10；T12 可以提前开工。T4、T6 必须等第一阶段合并。

---

## 9. 需要用户定的问题（附默认建议）

1. **FTS5 线上确认**：要不要上线前在 VPS 的 bot 容器里跑一条只读检查（需要运维授权）？默认不单独做，T12 的 healthcheck 告警会在下次部署时给出结果，失败时只有 /recall 降级。
2. **逐条反馈的形式**：按钮（每条 4 个，一次点击，状态可见）还是“每条一条消息 + 预置反应”？默认用按钮；反应作为任何消息上的辅助手段。
3. **共享频道里的反应**：所有者在共享卡片上手动加 🆕/👌/🚫，算不算数据？默认算，作为粗反馈按 1/n 加权，只进统计和周报，不影响共享选编。
4. **推送频道**：个人推送（following、追踪、周报、/recall）都发到 `INBOX_CHANNEL_ID` 吗？默认是；可选设 `WATCH_CHANNEL_ID` 把追踪分流出去。
5. **Embedding**：默认不做，上线两周看 /recall 的零结果率再定。
6. **知识库保留期**：默认新闻 90 天、最多 6 万条；有曝光、有反馈或被收藏的 365 天；收件箱永久；丢弃的 30 天后移出索引。
7. **周报要不要加一段模型写的小结**（每周一次 Claude 调用，约 $0.02）？默认不加。
8. **/recall 额度用完时**：默认直接告知；可选改为零模型列出命中。
9. **夜间静默**：追踪命中半夜也立刻推吗？默认立刻推，靠 Discord 的频道通知设置控制。
10. **主题追踪做不做**：反馈闭环跑起来之后，追踪是否还需要？默认在 T6 上线两周后再定。T7–T11 不阻塞阶段 3（T15 没有追踪数据时跳过对应段落）。
11. **信源改名**：约定信源名就是稳定 ID，改名等于删旧源、加新源。默认如此。
12. **回流强度**：权重上下限 [0.25, 4]、先验强度 4、冷启动 15 条、探索名额 20%，这些数字是否合适？默认按此上线，4 周后看新知率趋势再调。
13. **Claude 密钥**：需要用户把 `ANTHROPIC_API_KEY` 写进 VPS 的 `/srv/discord-bot/runtime/runtime.env`（属于运维动作，不在代码任务内）。没写的话，所有个人线路自动走免费链，功能不受影响。
14. **服务端拒答回退（`fallbacks`）**：claude-api 的默认建议是对 `claude-opus-5-5` 开启 `fallbacks: "default"`。本设计默认关闭，理由是本地免费链已经兜底、美元预留更可控。要不要开？
15. **时机**：HANDOFF 里约定 10/19 回看之前不加功能。默认 T1–T6 现在做，因为周报里的新知统计正是 10/19 回看和砍信源需要的数据；T7 及以后按回看结论再排。
16. **板块落在哪**：个人源的板块由第一阶段写进 `FeedSource.category`（取值为板块 id）？默认如此，需要第一阶段的 agent 确认。
