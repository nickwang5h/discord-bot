---
name: maintain-architecture
description: Maintain and modify this low-cost Discord AI Bot safely. Use whenever inspecting, diagnosing, refactoring, fixing, testing, or changing any code, configuration, scheduled job, AI provider, feed, cache, automation, or documentation in this repository. Enforces architecture-first investigation, bounded AI cost, at-most-once Discord delivery, deterministic validation, regression tests, and architecture documentation updates.
---

# Discord Bot Maintenance

按以下顺序工作。不要跳步，也不要用猜测替代代码、日志或测试证据。

## 1. 建立事实基线

在规划或修改前完成：

1. 完整读取仓库根目录的 `arch.md`。
2. 读取用户提到的文件、日志和附件，不要只根据错误摘要判断。
3. 运行 `git status --short`，确认已有改动；把不属于当前任务的改动视为用户资产。
4. 用 `rg` 找入口、调用者、测试和配置；不要只改报错的最后一行。
5. 若涉及模型 ID、API、依赖版本、外部服务、价格、限额或 RSS 可用性，完整读取 `../verify-realtime-data/SKILL.md`，再通过项目脚本或官方接口实时验证。

只做诊断时保持只读。用户要求修复或实现时，才修改文件。

## 2. 先画清完整执行链

至少回答以下问题后再动手：

- 入口是什么：slash command、listener、`tasks.loop`、启动 hook 还是脚本？
- 输入来自哪里，在哪一步裁剪、去重和验证？
- 哪些操作可以安全重试？
- 哪一步产生外部副作用：Discord 发送、文件写入或远程请求？
- 失败如何传播：返回用户文本、抛异常、降级还是停止本轮？
- 同一个任务能否因启动、定时和手动命令而并发执行？
- 状态在成功前还是成功后更新？

修改应覆盖根因所在的完整链路，而不是只吞掉最后出现的异常。

## 3. 遵守模块边界

- `cogs/`：Discord 命令、事件、定时触发、Embed 组装和频道交互。
- `core/`：可复用的 provider、抓取、存储、重试、格式归一化与业务基础设施。
- `scripts/`：无 Discord Gateway 依赖的运维、诊断和自动验证。
- `scratch/`：回归测试和人工集成测试。
- `config.py` / `core/settings.py`：配置入口；密钥不得写入公开设置或日志。

不要为了小改动引入新框架。出现第二个调用者或明确的基础设施职责时，再把逻辑下沉到 `core/`。

## 4. 不得破坏的系统不变量

### 4.1 Discord 推送：生成可重试，发送只一次

定时内容必须按以下事务边界组织：

```text
single-flight lock
  -> 抓取和生成（允许重试）
  -> channel.send（一次）
  -> 成功后更新缓存/状态
```

- 使用 `core.jobs.run_delivery_job()` 复用该语义。
- 不要把 `channel.send()` 放进通用 retry 循环。
- 发送后的 reaction、清缓存或记状态失败，不得触发整条消息重发。
- 定时触发与管理员手动触发必须共享同一把锁。

### 4.2 AI 接口：机器输出和用户输出分开

- 用户可直接阅读的普通问答使用 `ask_ai()`；它可以在全部 provider 失败时返回友好提示。
- JSON 或其他机器可解析输出使用 `generate_ai()`；失败必须抛出 `AIServiceUnavailable`，绝不能把友好错误文本交给 `json.loads()`。
- 定时生成若继续使用 `ask_ai()`，必须设置 `raise_on_failure=True`。
- 校验模型输出的类型、必需字段、URL 白名单、数值范围和长度；不要因为启用了 JSON mode 就信任响应。
- 模型提供摘要和分类，RSS、网页或用户输入才是标题、URL 等来源事实的所有者。

### 4.3 免费额度：每一层都要有边界

任何自动 AI 流程都要检查：

- 输入条数和单条字符数；
- batch 大小和最大输出 token；
- provider 级总超时；
- 最大并发和 single-flight；
- 自动触发频率及启动时是否立即执行；
- provider 全部失败后是否停止剩余批次；
- 缓存能否避免相同内容重复生成。

HTTP 413 是 payload/预算问题，优先缩小请求；不要把相同 payload 无意义地轮询所有模型。HTTP 429 是限流，遵守 cooldown/backoff，不要快速重试制造更多请求。

### 4.4 输出格式：模型约束必须有代码兜底

对 bullet list、禁止表格、长度限制等要求使用两层保护：

1. Prompt 明确规定格式并给一个短例子。
2. 展示层做确定性归一化和截断。

不要仅靠换模型或重复强调 prompt 解决格式问题。复用 `core.utils.create_ai_embed()` 和 Markdown 表格转换逻辑。

### 4.5 存储：原子写入、schema 校验、可迁移

- JSON 状态使用 `core.storage.JsonStore`，不要手写直接覆盖文件。
- 读改写使用 `update()`，避免一次操作中多次读取造成竞态。
- 新 schema 写入前做归一化；加载时识别旧 schema。
- 删除或迁移旧缓存前统计影响范围。缓存等派生数据可以重建，但设置、密钥和用户数据不得擅自丢弃。
- 只有 Discord 发送成功后才能标记内容已推送。

### 4.6 网络和密钥

- 复用 `core.web_fetcher` 的 URL、redirect、大小和超时保护；不要新增绕过 SSRF 检查的网页下载路径。
- 不打印 token、API key、完整授权 header 或包含密钥的配置文件。
- 不要为了消除未使用功能的 optional warning 增加依赖；先确认功能确实需要。

## 5. 按症状定位根因

| 症状 | 首先检查 | 禁止采用的表面修复 |
|---|---|---|
| 同一日报出现两份 | `send()` 是否位于 retry 内；是否存在两个 loop/并发手动触发 | 在发送后简单加布尔变量 |
| `json.loads()` 收到警告文本 | 是否错误使用 `ask_ai()`；失败是否被吞掉 | 用正则把警告伪装成 JSON |
| Groq/OpenRouter 413 | prompt + 最大 completion token；batch 大小 | 原样切换更多模型 |
| 连续 429 | cooldown、任务并发、批次速度、启动补抓 | 立即循环重试 |
| 模型输出表格 | prompt 和展示层归一化是否同时存在 | 只在 prompt 里多写一次“禁止” |
| 每次重启大量抓取 | interval loop 是否启动即执行；缓存 schema 是否阻止去重 | 单纯延长 RSS 超时 |
| 缓存有记录但永不推送 | 评分字段、阈值、旧 schema、`pushed` 状态 | 无条件降低筛选阈值 |
| 任务长时间卡住 | timeout 是每模型还是整个 provider；候选池大小 | 无限增加 timeout |

## 6. 实现规则

1. 选择能修完整链路的最小改动集。
2. 优先复用现有抽象；不要复制 provider、抓取或发送逻辑。
3. 新增常量时给出业务含义，避免散落 magic numbers。
4. 对预期的外部故障记录简洁 warning/error；只有意外异常才打印 traceback。
5. 后台任务必须能取消；Cog unload 时停止自己启动的 loop。
6. 手动测试命令不得绕过生产路径，否则测试结果没有代表性。
7. 保留工作树中无关改动，不使用破坏性 Git 命令。

## 7. 必须补的测试

先写与故障最接近的回归测试，再运行完整验证。

- 修改 provider/AI fallback：测试 provider 顺序、失败传播、cooldown、timeout 或状态码语义。
- 修改定时推送：测试生成重试后仅发送一次、并发触发被跳过、发送后状态更新。
- 修改机器 JSON：测试错误文本不会进入解析器、未知 URL 被拒绝、字段和分数被归一化。
- 修改缓存：使用临时 `JsonStore` 测试去重、迁移和损坏输入；测试不得改写真实 `data/`。
- 修改展示格式：测试表格转换、bullet 输出、footer 和 Discord 长度。
- 新增/删除 Cog：测试所有扩展可加载和卸载。

验证顺序：

```bash
python -m unittest -v <最相关的测试模块>
python scripts/validate.py
```

只有涉及外部事实或用户明确要求在线验证时再运行：

```bash
python scripts/healthcheck.py --strict --live
```

在线检查失败时区分代码错误、服务限流和网络限制；不要把暂时性第三方故障伪装成测试通过。

## 8. 更新架构文档

以下任一情况必须同步更新 `arch.md`：

- 新增、删除或重命名 Cog、`core/` 模块、脚本或依赖；
- 改变数据流、provider 顺序、fallback、retry、timeout 或 delivery 语义；
- 改变定时频率、并发、缓存 schema、配置或安全边界；
- 新增重要测试或运维入口。

只改注释、测试数据或不影响设计的局部实现时，不要为了留痕而添加无意义段落。`arch.md` 描述当前事实，不记录修改历史。

## 9. 完成门槛

交付前逐项确认：

- [ ] 已读取 `arch.md`、相关调用链和当前 diff。
- [ ] 根因已修复，异常没有被静默吞掉。
- [ ] retry 不包含 Discord 发送等不可安全重复的副作用。
- [ ] 自动 AI 流程有输入、输出、并发、频率和超时边界。
- [ ] 模型输出经过代码校验或确定性归一化。
- [ ] 缓存/设置写入原子化，schema 变化有迁移策略。
- [ ] 相关回归测试和 `python scripts/validate.py` 均通过。
- [ ] 必要时已更新 `arch.md`。
- [ ] 最终报告列出改动、验证结果、未验证项和真实剩余风险。
