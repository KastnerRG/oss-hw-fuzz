#!/usr/bin/env bash
# Host-side Docker launcher; all installation discovery runs in the common image.
set -euo pipefail

action=${1:?expected start or tools}
shift
docker_cmd=("$@")
((${#docker_cmd[@]})) || docker_cmd=(docker)

normalize() {
    local value=$1
    case "$value" in
        '~') value=$HOME ;;
        '~/'*) value="$HOME/${value:2}" ;;
    esac
    [[ "$value" != *$'\n'* ]] || { echo 'Tool paths must not contain newlines.' >&2; return 1; }
    realpath -ms -- "$value"
}

join_paths() { local IFS=:; printf '%s' "$*"; }
csv_field() { local escaped=${1//\"/\"\"}; printf '"%s"' "$escaped"; }

declare -a mounts=() roots=() paths=() entries=()
declare -A seen=()
declare -A mounted=()
IFS=: read -r -a entries <<< "${TOOL_ROOTS:-}"
for entry in "${entries[@]}"; do
    [[ -n "$entry" ]] || continue
    entry=$(normalize "$entry")
    [[ -d "$entry" && ! -v 'seen[$entry]' ]] || continue
    seen[$entry]=1
    roots+=("$entry")
    source=$(realpath -e -- "$entry")
    # Preserve both the configured name and physical path for vendor symlinks.
    for destination in "$entry" "$source"; do
        [[ ! -v 'mounted[$destination]' ]] || continue
        mounted[$destination]=1
        mounts+=(--mount "type=bind,$(csv_field "source=$source"),$(csv_field "target=$destination"),readonly")
    done
done
IFS=: read -r -a entries <<< "${TOOL_PATHS:-}"
for entry in "${entries[@]}"; do
    [[ -n "$entry" ]] && paths+=("$(normalize "$entry")")
done
roots_value=$(join_paths "${roots[@]}")
paths_value=$(join_paths "${paths[@]}")
discover=("${docker_cmd[@]}" run --rm --network none "${mounts[@]}"
    -e "TOOL_ROOTS=$roots_value" -e "TOOL_PATHS=$paths_value"
    "${IMAGE:?IMAGE is required}" /usr/local/bin/tool-env)

if [[ "$action" == tools ]]; then
    exec "${discover[@]}" --format json
fi
[[ "$action" == start ]] || { echo "Unknown action: $action" >&2; exit 2; }

env_file=$(mktemp)
trap 'rm -f "$env_file"' EXIT
"${discover[@]}" --format env > "$env_file"
mapfile -d '' -t tool_env < "$env_file"
env_args=()
for entry in "${tool_env[@]}"; do
    [[ "$entry" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || { echo 'Invalid tool environment output.' >&2; exit 2; }
    env_args+=(-e "$entry")
done
read -r -a license_names <<< "${LICENSE_ENV_VARS:-}"
for entry in "${license_names[@]}"; do
    [[ "$entry" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo 'Invalid license environment variable name.' >&2; exit 2; }
    env_args+=(-e "$entry")
done

# Detect stale environments without removing a container or its results.
image_id=$("${docker_cmd[@]}" image inspect "$IMAGE" --format '{{.Id}}')
configuration=$({
    printf '%s\0' "$image_id" "$roots_value" "$paths_value" "${mounts[@]}"
    cat "$env_file" "${SECCOMP_PROFILE:?SECCOMP_PROFILE is required}"
} | sha256sum | cut -d ' ' -f 1)
label=org.oss-hw-fuzz.tool-configuration
if "${docker_cmd[@]}" container inspect "${CONTAINER:?CONTAINER is required}" >/dev/null 2>&1; then
    existing=$("${docker_cmd[@]}" inspect "$CONTAINER" --format "{{index .Config.Labels \"$label\"}}")
    if [[ "$existing" != "$configuration" ]]; then
        echo "Container $CONTAINER has a different image or tool configuration." >&2
        echo 'Copy any needed results, then use make restart to apply the current configuration.' >&2
        exit 1
    fi
    "${docker_cmd[@]}" start "$CONTAINER" >/dev/null
else
    "${docker_cmd[@]}" run --detach --init --name "$CONTAINER" \
        --label "$label=$configuration" --security-opt "seccomp=$SECCOMP_PROFILE" \
        "${env_args[@]}" "${mounts[@]}" --shm-size=1g "$IMAGE" sleep infinity
fi
