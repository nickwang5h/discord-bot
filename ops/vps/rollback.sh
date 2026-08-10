#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 || ! $1 =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "Usage: $0 <existing-image-git-tag>" >&2
    exit 2
fi

release=$1
repo_root=$(git rev-parse --show-toplevel)
runtime_dir=${DISCORD_BOT_RUNTIME_DIR:-/srv/discord-bot/runtime}
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
printf 'DISCORD_BOT_IMAGE_TAG=%s\nDISCORD_BOT_RUNTIME_DIR=%s\n' \
    "$release" "$runtime_dir" >"$candidate_env"
chmod 600 "$candidate_env"

compose=(docker compose --env-file "$candidate_env" -f "$compose_file")
"${compose[@]}" up -d --remove-orphans --no-build --wait --wait-timeout 90

container_id=$("${compose[@]}" ps -q bot)
for _attempt in {1..12}; do
    if docker logs "$container_id" 2>&1 | grep -q '机器人已上线'; then
        mv "$candidate_env" "$deploy_env"
        chmod 600 "$deploy_env"
        trap - EXIT
        echo "Discord Bot rolled back to release $release."
        "${compose[@]}" ps
        exit 0
    fi
    sleep 5
done

echo "Rollback container did not report Discord Gateway readiness." >&2
if [[ -f "$deploy_env" ]]; then
    docker compose --env-file "$deploy_env" -f "$compose_file" up -d --remove-orphans --no-build
fi
exit 1
