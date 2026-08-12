#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/vps.sh [--target USER@HOST] <command> [argument]

Commands:
  deploy              Pull main, build the Git-SHA image, and deploy it
  status              Show the remote Git revision and container status
  health              Run the strict offline health check in the container
  logs [LINES]        Follow Bot logs (default: 100 lines)
  video-logs [LINES]  Follow video sidecar logs (default: 100 lines)
  env                 Edit the private Discord Bot runtime environment
  curator-env         Edit the private Info Curator provider environment
  curator-settings    Edit the private Info Curator video provider settings
  media-env           Edit the private Media Transcriber Cookie environment
  images              List retained Bot and video-sidecar image tags
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
info_repo=${INFO_CURATOR_REPO:-/srv/info-curator/source}
media_repo=${MEDIA_TRANSCRIBER_REPO:-/srv/media-transcriber/source}
info_runtime=${INFO_CURATOR_RUNTIME_DIR:-/srv/info-curator/runtime}
media_runtime=${MEDIA_TRANSCRIBER_RUNTIME_DIR:-/srv/media-transcriber/runtime}
if [[ "$remote_repo" != /* || "$runtime_dir" != /* || "$info_repo" != /* || "$media_repo" != /* || "$info_runtime" != /* || "$media_runtime" != /* ]]; then
    echo "Remote repository and runtime paths must be absolute." >&2
    exit 2
fi

ssh_options=(-o BatchMode=yes -o ConnectTimeout=12)

case "$command" in
    deploy)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        local_root=$(git rev-parse --show-toplevel)
        info_local=${INFO_CURATOR_LOCAL_REPO:-$local_root/../info-curator}
        media_local=${MEDIA_TRANSCRIBER_LOCAL_REPO:-$local_root/../media-transcriber}
        for path in "$local_root" "$info_local" "$media_local"; do
            if [[ ! -d $path/.git || -n $(git -C "$path" status --porcelain) || $(git -C "$path" branch --show-current) != main ]]; then
                echo "All three local source repositories must be clean main checkouts." >&2
                exit 1
            fi
        done
        discord_sha=$(git -C "$local_root" rev-parse HEAD)
        info_sha=$(git -C "$info_local" rev-parse HEAD)
        media_sha=$(git -C "$media_local" rev-parse HEAD)
        printf -v remote_command \
            'cd %q && DISCORD_BOT_RUNTIME_DIR=%q INFO_CURATOR_REPO=%q MEDIA_TRANSCRIBER_REPO=%q INFO_CURATOR_RUNTIME_DIR=%q MEDIA_TRANSCRIBER_RUNTIME_DIR=%q DISCORD_BOT_EXPECTED_SHA=%q INFO_CURATOR_EXPECTED_SHA=%q MEDIA_TRANSCRIBER_EXPECTED_SHA=%q ./ops/vps/deploy.sh' \
            "$remote_repo" "$runtime_dir" "$info_repo" "$media_repo" "$info_runtime" "$media_runtime" "$discord_sha" "$info_sha" "$media_sha"
        exec ssh "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    status)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        ssh "${ssh_options[@]}" "$ssh_target" bash -s -- "$remote_repo" "$info_repo" "$media_repo" <<'REMOTE'
set -Eeuo pipefail
for entry in "discord-bot:$1" "info-curator:$2" "media-transcriber:$3"; do
    name=${entry%%:*}; repo=${entry#*:}
    if git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        printf '%s_git_head=%s %s_dirty_lines=%s\n' "$name" "$(git -C "$repo" rev-parse --short=12 HEAD)" "$name" "$(git -C "$repo" status --porcelain | wc -l)"
    else
        printf '%s_checkout=missing\n' "$name"
    fi
done
missing=0
for container in discord-bot-bot-1 discord-bot-video-summary-1; do
    if docker inspect "$container" >/dev/null 2>&1; then
        docker ps -a --filter "name=^/${container}$" --format 'container={{.Names}} image={{.Image}} status={{.Status}} ports={{.Ports}}'
        printf '%s_restart_policy=%s %s_restart_count=%s\n' "$container" "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$container")" "$container" "$(docker inspect -f '{{.RestartCount}}' "$container")"
    else
        printf '%s=missing\n' "$container"
        missing=1
    fi
done
exit "$missing"
REMOTE
        ;;
    health)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        exec ssh "${ssh_options[@]}" "$ssh_target" \
            'docker exec discord-bot-video-summary-1 python /app/video_summary_worker.py --healthcheck && docker exec discord-bot-bot-1 python scripts/healthcheck.py --strict'
        ;;
    logs|video-logs)
        lines=${1:-100}
        [[ $# -le 1 && "$lines" =~ ^[1-9][0-9]{0,4}$ ]] || {
            echo "Log line count must be an integer from 1 to 99999." >&2
            exit 2
        }
        container=discord-bot-bot-1
        [[ $command == video-logs ]] && container=discord-bot-video-summary-1
        exec ssh "${ssh_options[@]}" "$ssh_target" \
            docker logs --follow --tail "$lines" "$container"
        ;;
    env)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'install -d -m 700 %q; touch %q/runtime.env; chmod 600 %q/runtime.env; nano %q/runtime.env' "$runtime_dir" "$runtime_dir" "$runtime_dir" "$runtime_dir"
        exec ssh -t "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    curator-env)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'install -d -m 700 %q; touch %q/runtime.env; chmod 600 %q/runtime.env; nano %q/runtime.env' "$info_runtime" "$info_runtime" "$info_runtime" "$info_runtime"
        exec ssh -t "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    curator-settings)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'install -d -m 700 %q; if [ ! -f %q/settings-openrouter.toml ]; then install -m 600 %q/config/video-openrouter.example.toml %q/settings-openrouter.toml; fi; chmod 600 %q/settings-openrouter.toml; nano %q/settings-openrouter.toml' "$info_runtime" "$info_runtime" "$info_repo" "$info_runtime" "$info_runtime" "$info_runtime"
        exec ssh -t "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    media-env)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'install -d -m 700 %q; touch %q/runtime.env; chmod 600 %q/runtime.env; nano %q/runtime.env' "$media_runtime" "$media_runtime" "$media_runtime" "$media_runtime"
        exec ssh -t "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    images)
        [[ $# -eq 0 ]] || { usage >&2; exit 2; }
        printf -v remote_command 'docker image ls --format %q | grep -E %q || true' \
            'image={{.Repository}}:{{.Tag}} created={{.CreatedSince}} size={{.Size}}' \
            '^image=discord-(bot|video-summary):'
        exec ssh "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    rollback)
        [[ $# -eq 1 && $1 =~ ^[0-9a-f]{7,40}$ ]] || {
            echo "rollback requires an existing Git-SHA image tag." >&2
            exit 2
        }
        release=$1
        printf -v remote_command \
            'cd %q && DISCORD_BOT_RUNTIME_DIR=%q INFO_CURATOR_REPO=%q MEDIA_TRANSCRIBER_REPO=%q INFO_CURATOR_RUNTIME_DIR=%q MEDIA_TRANSCRIBER_RUNTIME_DIR=%q ./ops/vps/rollback.sh %q' \
            "$remote_repo" "$runtime_dir" "$info_repo" "$media_repo" "$info_runtime" "$media_runtime" "$release"
        exec ssh "${ssh_options[@]}" "$ssh_target" "$remote_command"
        ;;
    *)
        echo "Unknown command: $command" >&2
        usage >&2
        exit 2
        ;;
esac
