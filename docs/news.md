# 新闻专题与订阅

新闻仍运行在同一个 Bot 进程。信源提供原始证据，专题负责选稿、提取、校验和卡片，订阅决定范围、时间和目标频道。`advanced_news` 是展示频道的用途，不是处理器类型。

## 上线后需要配置什么

沿用旧频道和现有 AI provider 时，**不需要新增 API Key、数据库服务或必填配置项**。

1. 管理员运行 `/set_news_channel channel:<综合新闻频道>`，仅在旧绑定缺失或需要换频道时设置。
2. 管理员运行 `/set_test_news_channel channel:<advanced_news频道>`，仅在旧绑定缺失或需要换频道时设置。默认视野拾遗与强电动态共用这个展示频道。
3. 确保 Bot 在目标频道拥有 View Channel、Send Messages、Embed Links 权限；命令调用者需管理员权限。人工核查命令读取历史消息时还需要 Read Message History。
4. `/news_status` 应显示“历史初始化：完成”；否则不要用 `--fresh` 绕过旧历史迁移。
5. 可以分别运行 `/news_preview subscription:general`、`/news_preview subscription:discovery`、`/news_preview subscription:power-us-ca`。预览只对调用者可见，会使用共享模型额度；无合适原始证据时可以没有卡片。

默认按 `BOT_TIMEZONE`（未改动时为 America/Toronto）运行：综合新闻08:45／15:30，视野拾遗08:00／18:00，美加强电动态12:00。`BOT_ENABLE_SCHEDULED_JOBS=true` 才启动定时循环；此开关属于部署配置，不通过聊天输入密钥或整个 env 文件。

只有需要自定义地区、时刻、频道或预算时，才设置下面的 `NEWS_SUBSCRIPTIONS`／`NEWS_LIMITS`。不要为了试用先写一份只有电力专题的替换列表，否则默认两个旧订阅会被移除。真实迁移及恢复定时需要按本文切换步骤执行；完成部署本身不等于已经验证真实 RSS 或模型出稿质量。

## 配置

配置继续由 `core.settings` / `JsonStore` 管理；运行状态单独放在 `<STATE_ROOT>/data/news.sqlite3`。不要直接覆盖运行中的 JSON 文件，也不要把订阅配置写进 SQLite。

未设置 `NEWS_SUBSCRIPTIONS` 时使用以下默认订阅，时间均为 `BOT_TIMEZONE`：

| ID | 专题 | 信源组 | 时间 | 兼容频道设置 |
|---|---|---|---|---|
| `general` | 综合新闻 | `general` | 08:45、15:30 | `NEWS_CHANNEL_ID` |
| `discovery` | 视野拾遗 | `discovery` | 08:00、18:00 | `TEST_NEWS_CHANNEL_ID` |
| `power-us-ca` | 强电动态 | 两组并集 | 12:00 | `TEST_NEWS_CHANNEL_ID` |

`NEWS_SUBSCRIPTIONS` 是**完整替换**默认列表，不是补丁。最多16份，每份每天1–4个 `HH:MM` 时刻。示例：

```json
{
  "id": "power-europe",
  "topic": "power_projects",
  "source_groups": ["general", "discovery"],
  "params": {"countries": ["GB", "DE", "FR", "IT", "ES"]},
  "times": ["12:00"],
  "channel_id": "123456789012345678",
  "max_candidates": 40,
  "max_output_tokens": 3000,
  "enabled": true
}
```

这是列表中的一项；要保留旧订阅，应连同旧订阅一起保存。可以通过现有 `core.settings.set_setting("NEWS_SUBSCRIPTIONS", subscriptions)` 原子更新公开配置。未配置频道的订阅不自动发送。错误项单独停用，同名 ID 冲突时所有同名项停用，其余订阅继续工作。

国家参数目前接受 `US`、`CA`、`GB`、`DE`、`FR`、`IT`、`ES`；欧洲示例仅覆盖列出的国家，不代表全欧洲。国家需有明确原文依据，暂不根据城市、公司总部或读者地区猜测。综合新闻和视野拾遗目前无范围参数。

ID 是持久投递身份。改频道、规则版本或范围不会清空历史；不要靠更换 ID 重试旧消息。真正独立的订阅使用新 ID。频道本身不决定专题。

`NEWS_LIMITS` 控制新闻子系统共享的资源上限，加载入口时读取，修改后需重载入口：

```json
{"concurrency": 2, "daily_calls": 24, "daily_output_tokens": 72000}
```

并发最多2；每天逻辑生成调用最多48，输出 token 预留总量最多144000。每次生成（包括预览、失败和 build 重试）均持久化预留整次 `max_output_tokens`，跨重启不恢复额度。单次配置范围256–3000；输入最多60000字符，模型响应最多24000字符，整次调用超时180秒。日期按 UTC 结算。这里计量的是 `generate_ai` 逻辑调用和输出上限，不是账单：provider 内部 fallback 可能尝试多个模型，不声明精确累计计费 token。

## 数据与专题边界

- `core/news/sources.py` 显式登记既有 RSS 组。RSS URL 去重后统一采集，每源最多40条、72小时窗口；不调用模型。采集有进程内锁和一小时间隔，启动不立即执行小时任务，但出刊或手动请求可共用首次采集。
- `ingest.py` 仅保存清洗后的原始标题、RSS 内容、链接、来源、发布时间、首次发现时间及内容版本。没有发布时间时，不将首次发现时间展示成发布时间。不同链接不会在采集层按标题或疑似事件合并。
- SQLite 保留最多3000条当前素材、6000个不可变原始版本，窗口7天。内容更新不重置首次发现时间。专题处理保存规则版本、参数、候选素材版本、已校验结果；处理缓存7天，并包含选编上下文，不能将美加缓存当成欧洲结果。
- 个人订阅 `following`（专题 `following`，信源组 `following`）只投递到 `INBOX_CHANNEL_ID`，默认 08:20／18:20，每期最多5条。信源清单见下文“个人信源”；个人信源不进入共享的综合新闻、视野拾遗和强电动态。订阅配置把 `following` 组放进共享专题、或投递到收件箱以外的频道，会被判为无效配置。
- 综合新闻保持8个来源（2026-10-05 移除已停更的 WSJ Markets）及最多36候选、每发布方最多4候选；视野拾遗保持9个来源、按发布方交错最多40候选、最多5张推荐。采集额度不受这两个专题的模型候选限制影响。
- 专题只能以原始证据为候选。历史只用于比较是否重复，另一专题的摘要、旧评分或推荐理由不会进入原始素材。
- 强电动态最多4条，区分规划／审批、公开采购、授标／签约、政策。项目名、地区、主体、设备和新增事实需要原文片段；不存在的字段显示未提供。混杂或已授标的报道不能输出为待采购。事件日期不得用发布时间代替。
- 强电动态按“国家＋项目名＋阶段＋原文明示事件日期”生成与媒体链接无关的投递键；没有事件日期时，同项目同阶段保守只发一次。新阶段或有明确日期的新里程碑可再次选编。同链接更新必须标明“新进展”。

语义判断不是数据库能保证的：项目别名、跨媒体的不同命名以及“是否真的构成新增事实”仍依赖模型比较原文与历史；逐字引文校验不能证明中文摘要的每项语义完全准确。缺少国家或阶段证据会少报，RSS 覆盖也不等于行业完整覆盖。本实现不做网页爬虫、存量买家搜索或采购机会真实性核验。

## 个人信源

清单在 `<STATE_ROOT>/data/personal_sources.json`（VPS 即 `BOT_STATE_DIR` 下的 `data/`），每次采集和出刊时重新读取，改完不用重启或重新部署。文件不存在时使用代码里的默认清单；文件损坏时也回落到默认清单，并在 `/source_list` 显示错误。单项无效只停用该项。最多30个。

```json
{
  "version": 1,
  "sources": [
    {"name": "CBC Ottawa", "category": "Ottawa", "url": "https://www.cbc.ca/webfeed/rss/rss-canada-ottawa"},
    {"name": "华尔街见闻", "category": "Investing", "rsshub": "/telegram/channel/cnwallstreet"},
    {"name": "B站关注", "category": "General", "rsshub": "/bilibili/followings/video/{uid}"}
  ]
}
```

- `category` 是板块，只能是 `Ottawa`、`Sudbury`、`Investing`、`AI-Tech`、`General`；选编提示按板块说明取舍，不设板块配额。
- `rsshub` 与 `url` 二选一。`rsshub` 只存路由（以 `/` 开头，不带查询参数），采集时拼上运行环境的 `RSSHUB_URL` 和 `RSSHUB_ACCESS_KEY`；文件里不出现访问密钥。没设这两个变量时 RSSHub 项不采集；`{uid}` 还需要 `BILIBILI_UID`。
- `url` 只接受不含登录信息的 https 公网地址，不能是本机、内网、私网 IP，也不能是 RSSHub 自己的域名（RSSHub 请填路由）。
- 名称必须唯一，且不能与共享信源同名（素材按来源名归属专题）。

Discord 管理命令（仅机器人所有者，回复只对调用者可见）：

- `/source_list`：按板块列出当前个人信源和配置错误。
- `/source_add name:<名称> address:<地址> section:<板块>`：地址可填 RSSHub 路由（如 `/twitter/user/名字`、`/telegram/channel/频道名`），也可直接粘贴 RSSHub 完整链接（会只保留路由、丢弃 key），或 https 公网 RSS。添加前不跟随跳转地试取一次，取不到或不是 RSS/Atom 就不写入。
- `/source_remove name:<名称>`：删除；已采集的该来源素材不再进入后续选编。

`following` 选编前先去重：同一标题（忽略大小写、全半角、标点和空白）在不同来源转发只留最新一条；已投递过的链接或标题不再进入候选，同链接改版也不再推送。

### 反馈怎么影响选编

`following` 每期消息里的条目按 ①…⑤ 编号，每条一行按钮 🆕 新知 / 👌 已知 / 🚫 不关心 / 📥 存收件箱（只认所有者，见 `arch.md` §6.7；反馈 Cog 没加载时照常发送，只是不带按钮）。这些反馈只影响 `following`，共享的综合新闻、视野拾遗和强电动态不读画像。

1. **画像**：每次出刊前读最近 90 天的有效反馈（半衰期 30 天），算出每个来源、板块、主题标签的权重（0.25–4，新知多的高、已知和不关心多的低）。精确反馈不足 15 条时没有画像，选编与没有反馈时一样。
2. **候选排序**（最多 40 条）：每个有候选的板块先保底 3 条（不足 3 条就全给），其余名额按“板块权重 × 候选数”分；板块内各来源按权重交错，来源内从新到旧。至少 20% 的名额留给评价还不足 5 次的来源，权重压到 0.25 的来源也至少留 1 条，让统计有机会回升。
3. **提示词**：把画像（已熟悉的主题、觉得新的主题、不关心的主题、各来源新鲜程度）和常用标签词表放进模型输入，最多 2,500 字符；提示里说明画像同样是不可信数据，只用于取舍。模型给每条打 1–3 个主题标签，标签随投递保存，供下一轮统计。
4. **后处理**：所有标签都是“强不关心”（不关心 ≥3 次且从未判过新知/已知）的条目丢掉；所有标签都是“已熟悉”且摘要不以“新进展：”开头的条目排到最后。
5. **模型**：`following` 先走 Claude（`personal.following`，结构化 JSON，每天最多 4 次、受 `CLAUDE_LIMITS` 美元上限约束），Claude 不可用、超预算或输入超过 30,000 字符时同一次调用里自动改走免费模型链，提示词和校验相同。候选会被收紧到输入不超过 30,000 字符。

`/feedback_stats` 每行显示该来源/板块/标签的当前权重，消息末尾附当前发给模型的画像片段。画像变化会让处理缓存失效并重新选编，但不影响投递身份（不会重发）。

## 命令与发送语义

- `/news_preview subscription:<ID>`：私密预览；可采集、消耗共享模型额度、保存处理缓存，但不创建正式运行或投递历史。
- `/news_publish subscription:<ID>`：管理员发布到该订阅配置频道，复用最近一期的运行键。
- `/news_status`：显示初始化门禁、有效订阅和待核查运行编号。
- `/test_news`：兼容综合新闻测试入口，改为当前交互私密预览，不污染正式历史。
- `/test_hourly_fetch`：兼容采集入口，共用一小时间隔，不调用模型。
- `/test_scheduled_digest`：兼容视野拾遗在当前频道发布，仍使用其订阅和本期历史，不能用它绕过防重。

每份订阅独立 single-flight；调度启动独立任务，慢专题不会阻塞下一时刻的其他订阅。模型并发和预算仍共享。没有候选不调用模型，没有合适选择不发送。定时器只处理当前时刻，不补发停机期间的旧期次；手动和定时使用相同的本地日期／时刻运行键，DST 重复小时也不是新一期。

正式发送步骤：

1. SQLite 声明 `(subscription, period)` 运行身份；未初始化历史、期次已存在或订阅有不确定发送时拒绝开始。
2. `run_delivery_job` 仅重试构建，最多2次；发送不重试。
3. 在 Discord 调用前，持久化 `sending` 和本次选中内容。
4. 单次 `channel.send(embeds=...)` 成功且返回消息 ID 后，以本地事务登记投递历史及 `delivered`。
5. 超时、取消、发送后本地记账失败进入 `uncertain`。重启将遗留 `sending` 转为 `uncertain`，遗留 `building` 转为 `failed`，不恢复发送。

不确定状态阻止该订阅后续期次，其他订阅不受影响。管理员先在对应频道人工核查，再使用 `/news_resolve run_id:<编号> message_id_or_skip:<消息ID>` 确认；程序验证消息来自本 Bot 且在本次运行之后，但管理员仍须确认是这一份新闻。无法确认或决定放弃时传 `skip`：保留该批内容的抑制身份，不伪装为成功、不自动补发。它不是“重试”按钮。

投递身份不会随原始素材或处理缓存的清理而删除。SQLite 与 Discord 不在同一事务，不能承诺严格恰好一次；设计选择不盲目重发，以少量可能漏报换取防止重复。

## 从旧入口切换（需要单独的运维授权）

以下操作在仓库根目录执行，**不属于仅修改代码时自动执行的步骤**。整个 Bot 只能有一个新闻运行实例。

1. 停止旧 Bot／旧定时入口。备份公开设置、两份旧新闻历史和现有新闻 SQLite（如有）；保留旧文件用于回滚，不覆盖、不删除。
2. 使用停机后的历史快照先做只读校验：

   ```bash
   python scripts/migrate_news.py --state-dir /absolute/state-root \
     --general-history /absolute/snapshot/news_digest_history.json \
     --discovery-history /absolute/snapshot/news_cache.json \
     --general-channel 123456789012345678 --discovery-channel 234567890123456789
   ```

3. 校验成功、确认 Bot 停止后，加 `--apply` 执行导入。工具只写新闻 SQLite，不更改旧 JSON 或公开设置，不连接 Discord／模型。导入原子且可重复执行，不重复添加投递身份。真实无历史的新安装才使用 `--fresh --apply`；迁移不能用 `--fresh` 绕过缺失历史。
4. 部署只含 `cogs/news.py` 的新入口。它自动复用旧频道设置。原 `news_digest.py`、`advanced_news.py` 已从代码中移除，不能在同一进程另外加载旧扩展。
5. 查看 `/news_status`，再用预览检查；批准后按原部署开关恢复定时。未完成显式初始化时，正式发送始终关闭。

旧综合新闻的原始摘要和已知版本身份用于比较；视野拾遗有原始内容时导入对应版本，没有原始内容时按链接保守抑制，仅影响原订阅。旧模型摘要不作为原文。旧探索缓存的 `timestamp` 是采集时间，不冒充发送时间；未知投递时间按导入时间排序，并保留 legacy 标记。旧未投递素材由统一 RSS 采集重新取得，不导入评分结果。

回滚时不要直接启用只认识旧 JSON 的旧新闻任务：新入口投递的记录不在旧文件中。先保持定时关闭，人工确认或制定反向迁移，避免回滚重发。

## 增加普通专题

在 `core/news/topics/` 添加实现并在 `topics/__init__.py` 显式注册，提供 `validate_params`、`prepare`、`validate`、`identity`、`render` 和规则版本、素材窗口；随后添加订阅配置及测试。`prepare` 只构造候选与提示，`validate` 校验专题自己的字段，`identity` 定义事件／里程碑去重，`render` 负责展示。不要在专题调用模型、采集、存储或 Discord 发送。已有 RSS 类型只调整信源组配置；不用修改公共流水线、调度或投递模块。
