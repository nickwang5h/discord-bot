#!/usr/bin/env bash
set -Eeuo pipefail

ssh_target=${DISCORD_BOT_SSH_TARGET:?Set DISCORD_BOT_SSH_TARGET (for example, user@Tailscale-host)}
remote_repo=${DISCORD_BOT_REMOTE_REPO:-/srv/discord-bot/repo}

printf -v remote_command 'cd %q && ./ops/vps/deploy.sh' "$remote_repo"
exec ssh -o BatchMode=yes "$ssh_target" "$remote_command"
