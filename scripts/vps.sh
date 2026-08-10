#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/vps.sh [--target USER@HOST] <command> [argument]

Commands:
  deploy              Pull main, build the Git-SHA image, and deploy it
  status              Show the remote Git revision and container status
  health              Run the strict offline health check in the container
  logs [LINES]        Follow container logs (default: 100 lines)
  env                 Edit the private VPS runtime environment with nano
  images              List locally retained Discord Bot image tags
  rollback <GIT_TAG>  Switch to an existing Git-SHA image tag
  help                Show this help

Target precedence:
  1. --target USER@HOST
  2. DISCORD_BOT_SSH_TARGET
  3. ~/.config/discord-bot/vps-target (one line, mode 600)
  4. SSH config alias: discord-bot-vps
EOF
}

ssh_target=""
if [[ ${1:-} == "--target" ]]; then
    [[ $# -ge 3 ]] || { usage >&2; exit 2; }
    ssh_target=$2
    shift 2
fi

command=${1:-help}
shift || true

case "$command" in
    help|-h|--help)
        usage
        exit 0
        ;;
esac

if [[ -z "$ssh_target" ]]; then
    ssh_target=${DISCORD_BOT_SSH_TARGET:-}
fi

target_file=${DISCORD_BOT_TARGET_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/discord-bot/vps-target}
if [[ -z "$ssh_target" && -f "$target_file" ]]; then
    mode=$(stat -c '%a' "$target_file")
    if (( (8#$mode & 8#077) != 0 )); then
        echo "$target_file must not be accessible by group or other users." >&2
        exit 1
    fi
    IFS= read -r ssh_target <"$target_file"
fi
ssh_target=${ssh_target:-discord-bot-vps}

if [[ "$ssh_target" == -* || "$ssh_target" =~ [[:space:]] ]]; then
    echo "Invalid SSH target." >&2
    exit 2
fi

remote_repo=${DISCORD_BOT_REMOTE_REPO:-/srv/discord-bot/repo}
runtime_dir=${DISCORD_BOT_RUNTIME_DIR:-/srv/discord-bot/runtime}
if [[ "$remote_repo" != /* || "$runtime_dir" != /* ]]; then
    echo "Remote repository and runtime paths must be absolute." >&2
    exit 2
fi

ssh_options=(-o BatchMode=yes -o ConnectTimeout=12)

case "$command" in
    deploy)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command \
            'cd %q && DISCORD_BOT_RUNTIME_DIR=%q ./ops/vps/deploy.sh' \
            "$remote_repo" "$runtime_dir"
        exec ssh "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    status)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        ssh "${ssh_options[@]}" "$ssh_target" bash -s -- "$remote_repo" <<'REMOTE'
set -Eeuo pipefail
repo=$1
container=discord-bot-bot-1
printf 'git_head=%s\n' "$(git -C "$repo" rev-parse --short=12 HEAD)"
printf 'git_dirty_lines=%s\n' "$(git -C "$repo" status --porcelain | wc -l)"
if docker inspect "$container" >/dev/null 2>&1; then
    docker ps -a --filter "name=^/${container}$" --format 'container={{.Names}} image={{.Image}} status={{.Status}} ports={{.Ports}}'
    printf 'restart_policy=%s\n' "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$container")"
    printf 'restart_count=%s\n' "$(docker inspect -f '{{.RestartCount}}' "$container")"
else
    echo 'container=missing'
    exit 1
fi
REMOTE
        ;;
    health)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        exec ssh "${ssh_options[@]}" "$ssh_target" \
            docker exec discord-bot-bot-1 python scripts/healthcheck.py --strict
        ;;
    logs)
        lines=${1:-100}
        [[ $# -le 1 && "$lines" =~ ^[1-9][0-9]{0,4}$ ]] || {
            echo "Log line count must be an integer from 1 to 99999." >&2
            exit 2
        }
        exec ssh "${ssh_options[@]}" "$ssh_target" \
            docker logs --follow --tail "$lines" discord-bot-bot-1
        ;;
    env)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'nano %q/runtime.env' "$runtime_dir"
        exec ssh -t "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    images)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'docker image ls discord-bot --format %q' \
            'tag={{.Tag}} created={{.CreatedSince}} size={{.Size}}'
        exec ssh "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    rollback)
        [[ $# -eq 1 && $1 =~ ^[0-9a-f]{7,40}$ ]] || {
            echo "rollback requires an existing Git-SHA image tag." >&2
            exit 2
        }
        release=$1
        printf -v remote_command \
            'cd %q && DISCORD_BOT_RUNTIME_DIR=%q ./ops/vps/rollback.sh %q' \
            "$remote_repo" "$runtime_dir" "$release"
        exec ssh "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    *)
        echo "Unknown command: $command" >&2
        usage >&2
        exit 2
        ;;
esac
