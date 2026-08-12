#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 || ! $1 =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "Usage: $0 <existing-image-release-tag>" >&2
    exit 2
fi

release=$1
repo_root=$(git rev-parse --show-toplevel)
runtime_dir=${DISCORD_BOT_RUNTIME_DIR:-/srv/discord-bot/runtime}
info_repo=${INFO_CURATOR_REPO:-/srv/info-curator/source}
media_repo=${MEDIA_TRANSCRIBER_REPO:-/srv/media-transcriber/source}
info_runtime=${INFO_CURATOR_RUNTIME_DIR:-/srv/info-curator/runtime}
media_runtime=${MEDIA_TRANSCRIBER_RUNTIME_DIR:-/srv/media-transcriber/runtime}
video_state=${VIDEO_SUMMARY_STATE_DIR:-/srv/info-curator/runtime/state}
compose_file="$repo_root/ops/vps/compose.yaml"
deploy_env="$runtime_dir/deploy.env"

exec 9>"$runtime_dir/deploy.lock"
if ! flock -n 9; then
    echo "Another Discord Bot deployment is already running." >&2
    exit 1
fi

if ! docker image inspect "discord-bot:$release" >/dev/null 2>&1; then
    echo "Image discord-bot:$release is not available on this VPS." >&2
    exit 1
fi

candidate_env=$(mktemp "$runtime_dir/.rollback.env.XXXXXX")
trap 'rm -f "$candidate_env"' EXIT
printf '%s\n' \
    "DISCORD_BOT_IMAGE_TAG=$release" \
    "DISCORD_BOT_RUNTIME_DIR=$runtime_dir" \
    "INFO_CURATOR_BUILD_CONTEXT=$info_repo" \
    "MEDIA_TRANSCRIBER_BUILD_CONTEXT=$media_repo" \
    "INFO_CURATOR_RUNTIME_DIR=$info_runtime" \
    "MEDIA_TRANSCRIBER_RUNTIME_DIR=$media_runtime" \
    "VIDEO_SUMMARY_STATE_DIR=$video_state" \
    >"$candidate_env"
chmod 600 "$candidate_env"

restore_previous_release() {
    [[ -f $deploy_env ]] || return
    local previous_tag
    previous_tag=$(awk -F= '$1 == "DISCORD_BOT_IMAGE_TAG" {print $2}' "$deploy_env")
    if [[ $previous_tag =~ ^[0-9a-f]{7,40}$ ]] \
        && docker image inspect "discord-video-summary:$previous_tag" >/dev/null 2>&1; then
        docker compose --env-file "$deploy_env" -f "$compose_file" up -d --remove-orphans --no-build
    else
        local legacy_env
        legacy_env=$(mktemp "$runtime_dir/.legacy-restore.env.XXXXXX")
        printf 'DISCORD_BOT_IMAGE_TAG=%s\nDISCORD_BOT_RUNTIME_DIR=%s\n' \
            "$previous_tag" "$runtime_dir" >"$legacy_env"
        chmod 600 "$legacy_env"
        docker compose --env-file "$legacy_env" -f "$compose_file" up -d --no-deps --no-build bot
        docker compose --env-file "$legacy_env" -f "$compose_file" stop video-summary >/dev/null 2>&1 || true
        rm -f "$legacy_env"
    fi
}

compose=(docker compose --env-file "$candidate_env" -f "$compose_file")
worker_available=false
if docker image inspect "discord-video-summary:$release" >/dev/null 2>&1; then
    worker_available=true
    "${compose[@]}" up -d --remove-orphans --no-build --wait --wait-timeout 120
else
    echo "Release $release predates the video-summary sidecar; restoring the Bot image only."
    legacy_env=$(mktemp "$runtime_dir/.legacy-target.env.XXXXXX")
    printf 'DISCORD_BOT_IMAGE_TAG=%s\nDISCORD_BOT_RUNTIME_DIR=%s\n' \
        "$release" "$runtime_dir" >"$legacy_env"
    chmod 600 "$legacy_env"
    compose=(docker compose --env-file "$legacy_env" -f "$compose_file")
    "${compose[@]}" up -d --no-deps --no-build bot
    "${compose[@]}" stop video-summary >/dev/null 2>&1 || true
    rm -f "$legacy_env"
fi

container_id=$("${compose[@]}" ps -q bot)
for _attempt in {1..12}; do
    if docker logs "$container_id" 2>&1 | grep -q '机器人已上线'; then
        mv "$candidate_env" "$deploy_env"
        chmod 600 "$deploy_env"
        trap - EXIT
        echo "Discord Bot rolled back to release $release (video-summary=$worker_available)."
        docker compose --env-file "$deploy_env" -f "$compose_file" ps
        exit 0
    fi
    sleep 5
done

echo "Rollback container did not report Discord Gateway readiness." >&2
restore_previous_release
exit 1
