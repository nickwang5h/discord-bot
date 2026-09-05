# Discord AI Bot 安装入口

当前正式部署使用 VPS Docker Compose，包含 Bot 与视频总结 sidecar。不要再按旧版单文件上传、免费托管或 `nohup` 步骤部署；这些方式不包含现有视频组件与健康检查、回滚流程。

## 1. 准备 Discord 应用

在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用与 Bot，启用 `Message Content Intent`。邀请时选择 `bot` 和 `applications.commands`，按目标频道需要授予查看频道、发送消息、嵌入链接和读取历史消息等权限；不要为省事直接授予 `Administrator`。

Token 和 API Key 只在私密运行环境中填写，不放进聊天、Git、截图或命令参数。`.gitignore` 不能代替密钥保护。

## 2. 选择安装方式

### 本地开发与离线验证

按 [README 的安装步骤](README.md#安装) 创建虚拟环境、从 `requirements.lock` 安装依赖，并配置运行环境。当前 Bot 开发、CI 和 VPS 镜像使用 Python 3.13；视频 sidecar 使用独立的 Python 3.12 镜像。

至少配置 Discord Token 和一个 AI provider。普通生成优先 Groq 的 Qwen，Gemini 并非必配；使用 Gemini 原生搜索才需要相应配置。完整配置及存储位置见 [配置与密钥](README.md#配置与密钥)。

在仓库根目录执行不要求部署密钥的验证：

```bash
python scripts/validate.py --allow-missing-secrets
```

本地连接 Discord 前，确认没有其他实例使用同一 Token，并在本地运行环境设置 `BOT_ENABLE_SCHEDULED_JOBS=false`，避免测试时发送定时报。然后运行 `python bot.py`；不要为了测试额外启动第二个生产实例。

### VPS 正式部署

按 [VPS 部署与运维指南](docs/vps-deployment.md) 完成 SSH 目标、三个源码仓库和各自私密 runtime 的准备。该指南是部署、状态检查和回滚的维护入口。

三个源码仓库验证通过，提交并 push，且均为 clean `main` 后，在 Bot 仓库根目录运行：

```bash
./scripts/vps.sh deploy
./scripts/vps.sh status
./scripts/vps.sh health
```

部署会检查两个容器健康状态和 Discord Gateway ready，失败自动恢复上一组镜像。手动回滚方法见 [回滚](docs/vps-deployment.md#6-回滚)。不要额外使用 `nohup` 或其他守护进程重复启动 Bot。

## 3. 功能确认

在 Discord 中用 `/ping` 检查响应；需要实际生成测试时再用 `/ask`，这会消耗对应 provider 配额。管理员可通过 `/health` 查看配置与任务状态。

`/set_gemini_key` 是可选的管理员命令，不是安装必经步骤；它保存到私密的 `data/secrets.json`，而非可提交的 `settings.json`。VPS 优先使用部署指南中的私密 runtime 编辑入口。

B站视频总结还需要独立 sidecar；B站 Cookie 和视频模型凭据分别归 Media Transcriber 与 Info Curator 管理，不能填入 Discord runtime。详见 [VPS 目录布局](docs/vps-deployment.md#8-目录布局)。
