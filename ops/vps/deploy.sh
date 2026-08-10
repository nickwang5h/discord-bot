#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel)
runtime_dir=${DISCORD_BOT_RUNTIME_DIR:-/srv/discord-bot/runtime}
compose_file="$repo_root/ops/vps/compose.yaml"
deploy_env="$runtime_dir/deploy.env"

if [[ $(id -u) -ne 1000 || $(id -g) -ne 1000 ]]; then
    echo "Run this deployment as the VPS application owner (uid/gid 1000)." >&2
    exit 1
fi

mkdir -p "$runtime_dir/state/data"
chmod 700 "$runtime_dir" "$runtime_dir/state" "$runtime_dir/state/data"

exec 9>"$runtime_dir/deploy.lock"
if ! flock -n 9; then
    echo "Another Discord Bot deployment is already running." >&2
    exit 1
fi

if [[ -n $(git -C "$repo_root" status --porcelain) ]]; then
    echo "Refusing to deploy from a dirty VPS checkout." >&2
    exit 1
fi
if [[ $(git -C "$repo_root" branch --show-current) != "main" ]]; then
    echo "The VPS checkout must be on the main branch." >&2
    exit 1
fi
if [[ ! -f "$runtime_dir/runtime.env" ]]; then
    echo "Missing $runtime_dir/runtime.env; create it with mode 600 first." >&2
    exit 1
fi

env_mode=$(stat -c '%a' "$runtime_dir/runtime.env")
if (( (8#$env_mode & 8#077) != 0 )); then
    echo "runtime.env must not be accessible by group or other users." >&2
    exit 1
fi

if [[ ! -f "$runtime_dir/state/settings.json" ]]; then
    cp "$repo_root/settings.json" "$runtime_dir/state/settings.json"
fi
chmod 600 "$runtime_dir/state/settings.json"

git -C "$repo_root" fetch origin main
git -C "$repo_root" merge --ff-only origin/main

release=$(git -C "$repo_root" rev-parse --short=12 HEAD)
candidate_env=$(mktemp "$runtime_dir/.deploy.env.XXXXXX")
trap 'rm -f "$candidate_env"' EXIT
printf 'DISCORD_BOT_IMAGE_TAG=%s\nDISCORD_BOT_RUNTIME_DIR=%s\n' \
    "$release" "$runtime_dir" >"$candidate_env"
chmod 600 "$candidate_env"

compose_candidate=(docker compose --env-file "$candidate_env" -f "$compose_file")
"${compose_candidate[@]}" build

if ! "${compose_candidate[@]}" up -d --remove-orphans --wait --wait-timeout 90; then
    echo "Candidate failed its container health check; restoring the previous release." >&2
    if [[ -f "$deploy_env" ]]; then
        docker compose --env-file "$deploy_env" -f "$compose_file" up -d --remove-orphans --no-build
    else
        "${compose_candidate[@]}" stop bot
    fi
    exit 1
fi

container_id=$("${compose_candidate[@]}" ps -q bot)
ready=false
for _attempt in {1..12}; do
    if docker logs "$container_id" 2>&1 | grep -q '机器人已上线'; then
        ready=true
        break
    fi
    sleep 5
done

if [[ "$ready" != true ]]; then
    echo "Candidate did not report Discord Gateway readiness; restoring the previous release." >&2
    if [[ -f "$deploy_env" ]]; then
        docker compose --env-file "$deploy_env" -f "$compose_file" up -d --remove-orphans --no-build
    else
        "${compose_candidate[@]}" stop bot
    fi
    exit 1
fi

mv "$candidate_env" "$deploy_env"
chmod 600 "$deploy_env"
trap - EXIT

echo "Discord Bot release $release is running and Gateway-ready."
"${compose_candidate[@]}" ps
