# VPS 快速部署与运维

Discord Bot 以同一 Docker Compose 项目中的 Bot 与视频总结 sidecar 两个容器运行。两者只发起出站连接，内部 HTTP 只在 Compose 网络可见，不发布端口、不经过 Caddy，也不加入公开 ingress 网络。

## 1. 一次性配置 SSH 目标

推荐把目标放在用户目录，而不是公开仓库或每次命令的 shell history 中：

```bash
install -d -m 700 ~/.config/discord-bot
printf '%s\n' 'ubuntu@YOUR_TAILSCALE_HOST' \
  > ~/.config/discord-bot/vps-target
chmod 600 ~/.config/discord-bot/vps-target
```

也可以临时使用：

```bash
DISCORD_BOT_SSH_TARGET=ubuntu@YOUR_TAILSCALE_HOST ./scripts/vps.sh status
```

或在 `~/.ssh/config` 中创建 `discord-bot-vps` alias；没有环境变量和目标文件时，脚本默认使用该 alias。

## 2. 一次性准备视频组件

VPS 需要三个独立、同属 uid/gid 1000 的 clean checkout：

```text
/srv/discord-bot/repo
/srv/info-curator/repo
/srv/media-transcriber/repo
```

Info Curator 与 Media Transcriber 不会被复制进 Discord 仓库；Docker BuildKit 仅把各自
`src/` 作为 named build context 放入 Python 3.12 sidecar。部署脚本会对三个仓库执行
`fetch` + `merge --ff-only origin/main`，并以三个完整 SHA 的 manifest hash 作为共同镜像
标签。任何 checkout dirty、不是 `main` 或缺失都会 fail closed。

通过以下命令分别在 VPS 内编辑配置，值不要经过聊天或 Git：

```bash
./scripts/vps.sh curator-env       # Info Curator provider key
./scripts/vps.sh curator-settings  # 首次从 owner 的公开模板生成，再编辑 endpoint/model/硬限制
./scripts/vps.sh media-env         # Media Transcriber Bilibili Cookie
```

三个 runtime 目录必须为 `0700`，文件必须由 uid/gid 1000 拥有且为 `0600`。Discord
runtime 不再保存 Bilibili Cookie 或视频总结 provider key。

## 3. 日常快速部署

部署只接受三个仓库都已经 push 到各自远端 `main` 的提交：

```bash
# 分别在三个仓库运行其完整离线验证，确认 clean main 并 push
python scripts/validate.py --allow-missing-secrets
git status --short --branch
./scripts/vps.sh deploy
```

部署脚本会：

1. 拒绝 VPS dirty checkout 和并行部署；
2. fast-forward 三个 checkout 到各自 `origin/main`；
3. 按三 SHA manifest 构建或复用 Bot 与 sidecar 镜像；
4. 先启动健康的 sidecar，再以单实例方式重建 Bot；
5. 等待两个 healthcheck 和 Discord Gateway ready；
6. 失败时恢复上一组同标签镜像。

普通代码更新会复用依赖层，通常只需数秒构建。切换过程有短暂 Gateway 断线，不采用多副本，以免定时任务重复发送。

兼容入口仍可使用：

```bash
./scripts/deploy_vps.sh
```

## 4. 常用命令

```bash
# Git revision、容器状态、restart policy
./scripts/vps.sh status

# 严格离线健康检查；不调用模型生成
./scripts/vps.sh health

# 跟随最近 100 行日志；Ctrl+C 退出
./scripts/vps.sh logs

# 指定日志行数；视频 sidecar 使用 video-logs
./scripts/vps.sh logs 300
./scripts/vps.sh video-logs 300

# 查看保留的可回滚镜像
./scripts/vps.sh images
```

容器名固定为 `discord-bot-bot-1` 与 `discord-bot-video-summary-1`，重启策略均为 `unless-stopped`。Docker daemon 和容器会随 VPS 重启恢复。

## 5. 修改私密运行配置

```bash
./scripts/vps.sh env
```

该命令通过 Tailscale SSH 在 VPS 上直接打开：

```text
/srv/discord-bot/runtime/runtime.env
```

不要把密钥复制到仓库、Issue、聊天或命令行参数中。保存后执行部署以重建容器并加载新环境：

```bash
./scripts/vps.sh deploy
```

`BOT_ENABLE_SCHEDULED_JOBS=false` 可暂停所有自动日报、阅读和高级资讯循环，但保留管理员手动命令；重新设为 `true` 并部署即可恢复。修改该值不会触发启动补发。

## 6. 回滚

先查看现有镜像标签：

```bash
./scripts/vps.sh images
```

然后选择完整显示的三仓库 composite release tag：

```bash
./scripts/vps.sh rollback 345cb9583f4e
```

回滚同时切换 Bot 与同标签视频 sidecar，不覆盖 Discord state、Info Curator 字幕/总结 artifacts 或任何私密环境文件；引入 sidecar 前的 Bot-only 镜像仍可回滚，但该旧版本使用旧 B站路径。不要运行全局 `docker system prune`，否则可能删除回滚镜像。

## 7. 故障处理

### 部署显示 unhealthy 或没有 Gateway ready

```bash
./scripts/vps.sh status
./scripts/vps.sh logs 300
```

部署脚本通常会自动恢复上一镜像。若配置值刚被修改，按归属使用 `env`、`curator-env`、`curator-settings` 或 `media-env` 修正，再重新部署。

### Discord 中出现重复日报

立即确认本地或其他主机没有使用相同 Token 运行第二个 Bot。当前 at-most-once 和 single-flight 只保证单进程语义。

### B站字幕或总结不可用

先运行 `./scripts/vps.sh status` 与 `./scripts/vps.sh health`，确认 sidecar 健康。Cookie 只编辑
`/srv/media-transcriber/runtime/runtime.env`；provider key 只编辑
`/srv/info-curator/runtime/runtime.env`。当前只接受完整 BV 第一P链接，有字幕时才总结；不会
回退到 Bot 自己的 AI、轮换代理、绕 CAPTCHA、下载媒体或启用 ASR。失败 identity 的
attempt marker 会保留，避免 Discord 重复消耗模型请求。

## 8. 目录布局

```text
/srv/discord-bot/
├── repo/                    # clean main checkout
└── runtime/
    ├── runtime.env          # Discord token/provider，mode 600
    ├── deploy.env           # 当前三仓库 manifest 与镜像标签
    └── state/               # Discord JSON state
/srv/info-curator/
├── repo/                    # clean main checkout
└── runtime/
    ├── runtime.env          # 视频总结 provider key，mode 600
    ├── settings-openrouter.toml
    └── state/video-summaries/ # 私有字幕、quarantine、总结 artifacts
/srv/media-transcriber/
├── repo/                    # clean main checkout
└── runtime/runtime.env      # Bilibili Cookie，mode 600
```

Bot 没有公开 HTTP 端口。Caddy、RSSHub 和 Uptime Kuma 的网络与端口模型不因 Bot 部署而改变。
