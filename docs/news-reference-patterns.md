# 新闻专题架构：外部实现参考与后续配置化

## 状态与用途

这份笔记保存对 Beehive、News Agent、rss-ai-news-py 的源码／文档对照，供以后确有扩展需求时参考。**它不是当前实施要求，不授权继续重构，也不承诺任何专题都只需新增一个配置文件。**

当前实现与运行操作以 [arch.md](../arch.md) 和 [新闻操作说明](news.md) 为准。此次保存不变更当前运行代码。

以下链接指向项目默认分支或 HEAD，可能随上游变化；再次采用前应复核对应版本。本文不对许可证、活跃度、星标或线上可用性作持续保证。

## 结论

适合本 Bot 的方向是：

```text
共享信源采集 → 原始证据池
                  ↓
         独立专题：代码＋声明式配置
                  ↓
        独立订阅：范围／时间／频道
                  ↓
             Discord 投递
```

借鉴 Beehive 的显式行为定义、rss-ai-news-py 的编辑配置方式；保留本 Bot 已有的共享证据池、按订阅隔离的投递状态和不确定发送保护。无需更换产品或再次推倒公共流水线。

## Beehive：借鉴行为定义，不照搬存储边界

[项目](https://github.com/sinmentis/beehive)

已核对的实现：

- 来源适配器输出统一 `RawItem`：[domain/models.py](https://github.com/sinmentis/beehive/blob/HEAD/src/beehive/domain/models.py)。
- 冻结的 `ChannelDefinition` 集中声明排序、持久化、生命周期、展示和邮件事件：[channels/definitions.py](https://github.com/sinmentis/beehive/blob/HEAD/src/beehive/channels/definitions.py)。
- workflow 分为 Editorial（新闻阅读）、Monitor（库存／价格等可变状态）和 Tracker（有期限的条目与提醒）。这是行为类型，不是电力、半导体等内容分类。
- 数据库中 `sources.channel_id` 绑定 Channel，`items.source_id` 绑定来源；条目行同时包含 AI 分数、摘要等字段：[db/schema.sql](https://github.com/sinmentis/beehive/blob/HEAD/src/beehive/db/schema.sql)。
- 采集按 Channel 获取来源并处理该 Channel 的未评分条目：[collector/run_cycle.py](https://github.com/sinmentis/beehive/blob/HEAD/src/beehive/collector/run_cycle.py)。

因此，“统一 RawItem 接口”不能直接理解为“全局素材只采集一次，任意多个专题独立使用”。本 Bot 的目标仍应保持信源、专题、订阅分开，不重新把信源所有权或发送时间绑回专题。

值得借鉴：少量明确的行为定义、显式注册、来源／专题失败隔离。暂不引入其商品生命周期、阅读状态、Web 面板、邮件分组或后台深读 worker。

## rss-ai-news-py：借鉴编辑配置，不把字段清单当业务校验

[项目](https://github.com/Develata/rss-ai-news-py)

[category 示例](https://github.com/Develata/rss-ai-news-py/blob/HEAD/news_crawler/categories/_example.toml) 将以下内容集中在 TOML：

- 分类键、顺序；
- RSS 来源；
- 编辑 Prompt、输入字符限制；
- 日报标题、目录、条数、导读和评分标记选项。

[CategoryStrategy](https://github.com/Develata/rss-ai-news-py/blob/HEAD/news_crawler/core/category_strategies.py) 从配置构建策略。示例仍要求摘要、`TAGS`、`SCORE`，不能据此认定已支持任意业务结构及其语义校验。

例如，仅列出 `procurement_status` 和 `new_fact` 不能保证：

- 已授标不会被写成待采购；
- 新增事实有对应原文依据；
- 发布时间没有被冒充为事件时间；
- 换媒体链接不会重复播报同一里程碑。

即使使用真正的 JSON Schema，它主要约束结构、类型、枚举和必填项；跨字段业务约束及证据一致性仍需专题逻辑和测试。

## News Agent：参考完整流程，不直接替换 Bot

[项目及配置说明](https://github.com/huawolf/news-agent#configure)

其 README 描述了统一新闻池、兴趣／排除项／来源权重、LLM 排序与摘要，以及 Discord 等投递方式。配置说明明确指出：每次运行处理完整新闻来源池和 GitHub，不能按 delivery schedule 独立选择 sources。

这与本 Bot 需要的“共享原始证据 → 不同用途的专题 → 独立范围和历史的订阅”不完全相同。可参考配置体验和流程组织，不替换现有 AI provider、Discord 与调度体系。

## 后续若要配置化，边界如何划分

| 可放入声明式配置 | 保留在专题代码与测试中 |
|---|---|
| 名称、兴趣词、排除词 | 原文证据约束 |
| 编辑 Prompt、展示文案 | 规划／采购／授标状态一致性 |
| 候选量、输出预算 | 项目与里程碑去重 |
| 已实现处理器／模板的选择 | 新增事实和事件日期的约束 |
| 已支持来源的组合 | 异常与未知状态处理 |

地区、频率、目标频道仍属于订阅。例如，美加与欧洲电力动态应复用同一电力专题，不复制两份地区化专题实现。

当前电力专题中的兴趣关键词、编辑 Prompt 等频繁调整项，是未来可考虑抽出的配置；公共采集、调度、SQLite 和发送流程不因此改造。配置不能执行任意代码、加载任意模块或覆盖系统安全约束，也不应演变成工作流 DSL。

## “只新增一个配置文件”的适用范围

1. **新增欧洲电力订阅**：使用已支持国家和相同规则，仅新增订阅配置。
2. **同一种处理规则下新增内容栏目**：如果已有处理器、校验和展示模板足以覆盖，可以仅增加配置。
3. **新增半导体建厂与设备采购专题**：若阶段、设备类型、项目身份或采购规则不同，仍需专题实现／校验器及测试。

等电力与半导体两个实际专题证明规则相同，再考虑提炼共同的项目进展处理器。不要提前用一个万能 Prompt 或不断增长的配置条件隐藏业务差异。

**衡量标准：普通编辑调整不改代码；真正的新业务规则允许有代码。**
