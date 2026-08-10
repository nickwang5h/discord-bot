# VPS 快速部署与运维

Discord Bot 以单个 Docker Compose 容器运行在 VPS 上。它只发起出站连接，不发布端口、不经过 Caddy，也不加入公开 ingress 网络。

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

## 2. 日常快速部署

部署只接受已经 push 到远端 `main` 的提交：

```bash
python scripts/validate.py --allow-missing-secrets
git status --short --branch
git push origin main
./scripts/vps.sh deploy
```

部署脚本会：

1. 拒绝 VPS dirty checkout 和并行部署；
2. fast-forward 到 `origin/main`；
3. 按 Git SHA 构建或复用缓存镜像；
4. 以单实例方式重建容器；
5. 等待严格离线 healthcheck 和 Discord Gateway ready；
6. 失败时恢复上一镜像。

普通代码更新会复用依赖层，通常只需数秒构建。切换过程有短暂 Gateway 断线，不采用多副本，以免定时任务重复发送。

兼容入口仍可使用：

```bash
./scripts/deploy_vps.sh
```

## 3. 常用命令

```bash
# Git revision、容器状态、restart policy
./scripts/vps.sh status

# 严格离线健康检查；不调用模型生成
./scripts/vps.sh health

# 跟随最近 100 行日志；Ctrl+C 退出
./scripts/vps.sh logs

# 指定日志行数
./scripts/vps.sh logs 300

# 查看保留的可回滚镜像
./scripts/vps.sh images
```

容器名固定为 `discord-bot-bot-1`，重启策略为 `unless-stopped`。Docker daemon 和容器会随 VPS 重启恢复。

## 4. 修改私密运行配置

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

## 5. 回滚

先查看现有镜像标签：

```bash
./scripts/vps.sh images
```

然后选择完整显示的 Git-SHA tag：

```bash
./scripts/vps.sh rollback 345cb9583f4e
```

回滚只切换代码镜像，不覆盖 `/srv/discord-bot/runtime/state` 或私密环境文件。不要运行全局 `docker system prune`，否则可能删除回滚镜像。

## 6. 故障处理

### 部署显示 unhealthy 或没有 Gateway ready

```bash
./scripts/vps.sh status
./scripts/vps.sh logs 300
```

部署脚本通常会自动恢复上一镜像。若配置值刚被修改，先用 `./scripts/vps.sh env` 修正，再重新部署。

### Discord 中出现重复日报

立即确认本地或其他主机没有使用相同 Token 运行第二个 Bot。当前 at-most-once 和 single-flight 只保证单进程语义。

### B站字幕不可用

`BILIBILI_COOKIE` 是可选项。先测试匿名公开视频；只有确实需要登录态时才使用专用低权限账号 Cookie。不要通过轮换代理、绕 CAPTCHA 或下载媒体来规避平台控制。

## 7. 目录布局

```text
/srv/discord-bot/
├── repo/                    # clean main checkout
└── runtime/
    ├── runtime.env          # mode 600，私密环境
    ├── deploy.env           # 当前 Git-SHA 镜像标签
    └── state/
        ├── settings.json
        └── data/
            ├── secrets.json
            └── news_cache.json
```

Bot 没有公开 HTTP 端口。Caddy、RSSHub 和 Uptime Kuma 的网络与端口模型不因 Bot 部署而改变。
