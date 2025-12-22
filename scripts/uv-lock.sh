#!/usr/bin/env bash
set -euo pipefail

# `uv lock` / `uv sync` をコンテナ内で実行し, repo の `uv.lock` を更新するためのヘルパー.
#
# 例:
#   ./scripts/uv-lock.sh lock
#   ./scripts/uv-lock.sh sync
#   ./scripts/uv-lock.sh lock+sync
#
# Compose 構成を切り替えたい場合:
#   COMPOSE_FILES="docker-compose.full-build.webui-only.yaml docker-compose.seed-functions.yaml" ./scripts/uv-lock.sh lock
#
# 注意:
# - `uv` が Open WebUI の image 内に存在する前提です.
# - prebuilt image 利用時は, `uv lock` しても実行中コンテナに依存は反映されません（custom image の build が必要）.

command="${1:-lock}"

case "${command}" in
  lock|sync|lock+sync) ;;
  *)
    echo "Usage: $0 [lock|sync|lock+sync]" >&2
    exit 2
    ;;
esac

service_name="${UV_SERVICE_NAME:-open-webui}"
workspace_dir="${UV_WORKSPACE_DIR:-/workspace}"

compose_files_raw="${COMPOSE_FILES:-docker-compose.full-build.webui-only.yaml}"
read -r -a compose_files <<<"${compose_files_raw}"

compose_args=()
for compose_file in "${compose_files[@]}"; do
  compose_args+=(-f "${compose_file}")
done

run_uv() {
  docker compose "${compose_args[@]}" run --rm \
    -v "${PWD}:${workspace_dir}" \
    -w "${workspace_dir}" \
    "${service_name}" \
    uv "$@"
}

if [[ "${command}" == "lock" ]]; then
  run_uv lock
  exit 0
fi

if [[ "${command}" == "sync" ]]; then
  run_uv sync
  exit 0
fi

run_uv lock
run_uv sync
