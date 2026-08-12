#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(git rev-parse --show-toplevel)
runtime_dir=${DISCORD_BOT_RUNTIME_DIR:-/srv/discord-bot/runtime}
info_repo=${INFO_CURATOR_REPO:-/srv/info-curator/repo}
media_repo=${MEDIA_TRANSCRIBER_REPO:-/srv/media-transcriber/repo}
info_runtime=${INFO_CURATOR_RUNTIME_DIR:-/srv/info-curator/runtime}
media_runtime=${MEDIA_TRANSCRIBER_RUNTIME_DIR:-/srv/media-transcriber/runtime}
video_state=${VIDEO_SUMMARY_STATE_DIR:-/srv/info-curator/runtime/state}
compose_file="$repo_root/ops/vps/compose.yaml"
deploy_env="$runtime_dir/deploy.env"

if [[ $(id -u) -ne 1000 || $(id -g) -ne 1000 ]]; then
    echo "Run this deployment as the VPS application owner (uid/gid 1000)." >&2
    exit 1
fi

for path in "$runtime_dir" "$info_repo" "$media_repo" "$info_runtime" "$media_runtime" "$video_state"; do
    if [[ $path != /* || $path =~ [[:space:]] ]]; then
        echo "Deployment paths must be absolute and contain no whitespace." >&2
        exit 1
    fi
done

mkdir -p "$runtime_dir/state/data" "$video_state"
chmod 700 "$runtime_dir" "$runtime_dir/state" "$runtime_dir/state/data" "$video_state"

exec 9>"$runtime_dir/deploy.lock"
if ! flock -n 9; then
    echo "Another Discord Bot deployment is already running." >&2
    exit 1
fi

update_repo() {
    local name=$1
    local path=$2
    if ! git -C "$path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "$name checkout is missing at $path." >&2
        exit 1
    fi
    if [[ -n $(git -C "$path" status --porcelain) ]]; then
        echo "Refusing to deploy from dirty $name checkout: $path." >&2
        exit 1
    fi
    if [[ $(git -C "$path" branch --show-current) != "main" ]]; then
        echo "$name checkout must be on main: $path." >&2
        exit 1
    fi
    git -C "$path" fetch origin main
    git -C "$path" merge --ff-only origin/main
}

check_private_dir() {
    local path=$1
    if [[ ! -d $path || -L $path ]]; then
        echo "Missing owner-only runtime directory: $path." >&2
        exit 1
    fi
    local mode owner
    mode=$(stat -c '%a' "$path")
    owner=$(stat -c '%u:%g' "$path")
    if (( (8#$mode & 8#077) != 0 )) || [[ $owner != "1000:1000" ]]; then
        echo "$path must be owned by uid/gid 1000 and inaccessible to group/other." >&2
        exit 1
    fi
}

check_private_file() {
    local path=$1
    if [[ ! -f $path || -L $path ]]; then
        echo "Missing owner-only runtime file: $path." >&2
        exit 1
    fi
    local mode owner
    mode=$(stat -c '%a' "$path")
    owner=$(stat -c '%u:%g' "$path")
    if (( (8#$mode & 8#077) != 0 )) || [[ $owner != "1000:1000" ]]; then
        echo "$path must be owned by uid/gid 1000 and inaccessible to group/other." >&2
        exit 1
    fi
}

check_private_dir "$runtime_dir"
check_private_dir "$info_runtime"
check_private_dir "$media_runtime"
check_private_dir "$video_state"
check_private_file "$runtime_dir/runtime.env"
check_private_file "$info_runtime/runtime.env"
check_private_file "$info_runtime/settings-openrouter.toml"
check_private_file "$media_runtime/runtime.env"

if [[ ! -f "$runtime_dir/state/settings.json" ]]; then
    cp "$repo_root/settings.json" "$runtime_dir/state/settings.json"
fi
chmod 600 "$runtime_dir/state/settings.json"

update_repo "Discord Bot" "$repo_root"
update_repo "Info Curator" "$info_repo"
update_repo "Media Transcriber" "$media_repo"

discord_sha=$(git -C "$repo_root" rev-parse HEAD)
info_sha=$(git -C "$info_repo" rev-parse HEAD)
media_sha=$(git -C "$media_repo" rev-parse HEAD)
release=$(printf '%s\n%s\n%s\n' "$discord_sha" "$info_sha" "$media_sha" | sha256sum | cut -c1-12)

candidate_env=$(mktemp "$runtime_dir/.deploy.env.XXXXXX")
trap 'rm -f "$candidate_env"' EXIT
printf '%s\n' \
    "DISCORD_BOT_IMAGE_TAG=$release" \
    "DISCORD_BOT_RUNTIME_DIR=$runtime_dir" \
    "INFO_CURATOR_BUILD_CONTEXT=$info_repo" \
    "MEDIA_TRANSCRIBER_BUILD_CONTEXT=$media_repo" \
    "INFO_CURATOR_RUNTIME_DIR=$info_runtime" \
    "MEDIA_TRANSCRIBER_RUNTIME_DIR=$media_runtime" \
    "VIDEO_SUMMARY_STATE_DIR=$video_state" \
    "DISCORD_BOT_GIT_SHA=$discord_sha" \
    "INFO_CURATOR_GIT_SHA=$info_sha" \
    "MEDIA_TRANSCRIBER_GIT_SHA=$media_sha" \
    >"$candidate_env"
chmod 600 "$candidate_env"

restore_previous_release() {
    if [[ ! -f $deploy_env ]]; then
        "${compose_candidate[@]}" stop
        return
    fi
    local previous_tag
    previous_tag=$(awk -F= '$1 == "DISCORD_BOT_IMAGE_TAG" {print $2}' "$deploy_env")
    if [[ $previous_tag =~ ^[0-9a-f]{7,40}$ ]] \
        && docker image inspect "discord-video-summary:$previous_tag" >/dev/null 2>&1; then
        docker compose --env-file "$deploy_env" -f "$compose_file" up -d --remove-orphans --no-build
    else
        docker compose --env-file "$deploy_env" -f "$compose_file" up -d --no-deps --no-build bot
        docker compose --env-file "$deploy_env" -f "$compose_file" stop video-summary >/dev/null 2>&1 || true
    fi
}

compose_candidate=(docker compose --env-file "$candidate_env" -f "$compose_file")
"${compose_candidate[@]}" build

if ! "${compose_candidate[@]}" up -d --remove-orphans --wait --wait-timeout 120; then
    echo "Candidate failed its container health checks; restoring the previous release." >&2
    restore_previous_release
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
    restore_previous_release
    exit 1
fi

mv "$candidate_env" "$deploy_env"
chmod 600 "$deploy_env"
trap - EXIT

echo "Discord Bot composite release $release is running and Gateway-ready."
docker compose --env-file "$deploy_env" -f "$compose_file" ps
