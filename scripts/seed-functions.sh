#!/usr/bin/env bash
set -euo pipefail

WEBUI_URL="${WEBUI_URL:-http://open-webui:8080}"
WEBUI_SEED_API_KEY="${WEBUI_SEED_API_KEY:-}"
FUNCTIONS_DIR="${FUNCTIONS_DIR:-/seed/webui_functions}"
FUNCTIONS_SRC_DIR="${FUNCTIONS_SRC_DIR:-}"

trim_and_unquote() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"

  if [[ ${#value} -ge 2 ]]; then
    local first="${value:0:1}"
    local last="${value: -1}"
    if [[ ( "${first}" == "'" && "${last}" == "'" ) || ( "${first}" == "\"" && "${last}" == "\"" ) ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi

  printf '%s' "${value}"
}

WEBUI_SEED_API_KEY="$(trim_and_unquote "${WEBUI_SEED_API_KEY}")"

if [[ -z "${WEBUI_SEED_API_KEY}" ]]; then
  echo "Missing WEBUI_SEED_API_KEY (must be an admin user's sk-... API key)." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "Missing curl." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Missing jq." >&2
  exit 1
fi

if [[ ! -d "${FUNCTIONS_DIR}" ]]; then
  echo "FUNCTIONS_DIR not found: ${FUNCTIONS_DIR}" >&2
  exit 1
fi

auth_header="Authorization: Bearer ${WEBUI_SEED_API_KEY}"

wait_for_webui() {
  local attempt=0
  local max_attempts=60
  while (( attempt < max_attempts )); do
    if curl -fsS "${WEBUI_URL}/health" >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  echo "Open WebUI health check failed: ${WEBUI_URL}/health" >&2
  return 1
}

frontmatter_value() {
  local file="$1"
  local key="$2"
  awk -v k="${key}" '
    BEGIN { in_block=0 }
    NR==1 && $0 ~ /^"""/ { in_block=1; next }
    in_block==1 && $0 ~ /"""/ { exit }
    in_block==1 && $0 ~ "^[[:space:]]*" k ":[[:space:]]*" {
      sub("^[[:space:]]*" k ":[[:space:]]*", "", $0)
      print $0
      exit
    }
  ' "${file}"
}

frontmatter_truthy() {
  local file="$1"
  local key="$2"
  local value
  value="$(frontmatter_value "${file}" "${key}" || true)"
  value="$(trim_and_unquote "${value}")"
  case "${value}" in
    true|True|TRUE|1|yes|Yes|YES|on|On|ON) echo "true" ;;
    *) echo "false" ;;
  esac
}

ensure_no_requirements() {
  local file="$1"
  local req
  req="$(frontmatter_value "${file}" "requirements" || true)"
  if [[ -n "${req}" ]]; then
    echo "requirements frontmatter is not allowed for seeded functions: ${file}" >&2
    exit 1
  fi
}

http_get_json() {
  local url="$1"
  local out_file="$2"
  local status
  status="$(
    curl -sS -H "${auth_header}" -o "${out_file}" -w "%{http_code}" "${url}" || true
  )"
  echo "${status}"
}

http_post_json() {
  local url="$1"
  local body_file="$2"
  curl -sS -H "${auth_header}" -H "Content-Type: application/json" -X POST --data-binary @"${body_file}" "${url}" >/dev/null
}

bundle_functions_dir() {
  local out_dir="$1"

  if [[ -z "${FUNCTIONS_SRC_DIR}" || ! -d "${FUNCTIONS_SRC_DIR}" ]]; then
    return 0
  fi

  local python_bin="${PYTHON_BIN:-}"
  if [[ -z "${python_bin}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      python_bin="python3"
    elif command -v python >/dev/null 2>&1; then
      python_bin="python"
    else
      echo "Missing python3/python (required to bundle FUNCTIONS_SRC_DIR)." >&2
      return 1
    fi
  fi

  if [[ ! -f "/seed/bundle-function.py" ]]; then
    echo "Missing bundler script: /seed/bundle-function.py" >&2
    return 1
  fi

  mkdir -p "${out_dir}"

  local ids_tmp
  ids_tmp="$(mktemp)"

  (
    shopt -s nullglob
    for f in "${FUNCTIONS_DIR}"/*.py; do
      basename "${f}" .py | tr '[:upper:]' '[:lower:]'
    done
    for d in "${FUNCTIONS_SRC_DIR}"/*; do
      if [[ -d "${d}" ]]; then
        basename "${d}"
      fi
    done
  ) | sort -u > "${ids_tmp}"

  while IFS= read -r function_id; do
    if [[ -z "${function_id}" ]]; then
      continue
    fi

    if [[ -d "${FUNCTIONS_SRC_DIR}/${function_id}" ]]; then
      local src_dir="${FUNCTIONS_SRC_DIR}/${function_id}"
      "${python_bin}" /seed/bundle-function.py \
        --src-dir "${src_dir}" \
        --function-id "${function_id}" \
        --out-file "${out_dir}/${function_id}.py"
      continue
    fi

    if [[ -f "${FUNCTIONS_DIR}/${function_id}.py" ]]; then
      cp "${FUNCTIONS_DIR}/${function_id}.py" "${out_dir}/${function_id}.py"
      continue
    fi

    echo "Function source not found in either FUNCTIONS_SRC_DIR or FUNCTIONS_DIR: ${function_id}" >&2
    rm -f "${ids_tmp}"
    return 1
  done < "${ids_tmp}"

  rm -f "${ids_tmp}"
  return 0
}

wait_for_webui

shopt -s nullglob
bundled_dir=""
if [[ -n "${FUNCTIONS_SRC_DIR}" && -d "${FUNCTIONS_SRC_DIR}" ]]; then
  bundled_dir="$(mktemp -d)"
  trap 'rm -rf "${bundled_dir}"' EXIT
  bundle_functions_dir "${bundled_dir}"
  FUNCTIONS_DIR="${bundled_dir}"
fi

files=( "${FUNCTIONS_DIR}"/*.py )
if (( ${#files[@]} == 0 )); then
  echo "No .py files found under FUNCTIONS_DIR: ${FUNCTIONS_DIR}" >&2
  exit 1
fi

for file in "${files[@]}"; do
  function_id="$(basename "${file}" .py | tr '[:upper:]' '[:lower:]')"

  if [[ ! "${function_id}" =~ ^[a-z_][a-z0-9_]*$ ]]; then
    echo "Invalid function id (must be a Python identifier): ${function_id} (${file})" >&2
    exit 1
  fi

  ensure_no_requirements "${file}"

  name="$(frontmatter_value "${file}" "name" || true)"
  if [[ -z "${name}" ]]; then
    name="${function_id}"
  fi

  default_enabled="$(frontmatter_truthy "${file}" "default_enabled")"

  tmp_body="$(mktemp)"
  jq -n \
    --arg id "${function_id}" \
    --arg name "${name}" \
    --argjson default_enabled "${default_enabled}" \
    --rawfile content "${file}" \
    '{id: $id, name: $name, content: $content, meta: {manifest: {default_enabled: $default_enabled}}}' \
    > "${tmp_body}"

  tmp_get="$(mktemp)"
  status="$(http_get_json "${WEBUI_URL}/api/v1/functions/id/${function_id}" "${tmp_get}")"

  detail="$(jq -r '.detail // empty' < "${tmp_get}" 2>/dev/null || true)"

  if [[ "${status}" == "200" ]]; then
    http_post_json "${WEBUI_URL}/api/v1/functions/id/${function_id}/update" "${tmp_body}"
  elif [[ "${status}" == "401" && "${detail}" == "We could not find what you're looking for :/" ]]; then
    # Open WebUI returns 401 for "not found" on this endpoint.
    http_post_json "${WEBUI_URL}/api/v1/functions/create" "${tmp_body}"
  elif [[ "${status}" == "401" || "${status}" == "403" ]]; then
    echo "Unauthorized to manage functions. WEBUI_SEED_API_KEY must belong to an admin user, and API key auth must allow these endpoints." >&2
    cat "${tmp_get}" >&2 || true
    exit 1
  else
    http_post_json "${WEBUI_URL}/api/v1/functions/create" "${tmp_body}"
  fi

  tmp_state="$(mktemp)"
  status2="$(http_get_json "${WEBUI_URL}/api/v1/functions/id/${function_id}" "${tmp_state}")"
  if [[ "${status2}" != "200" ]]; then
    echo "Failed to read function after upsert: ${function_id} (status=${status2})" >&2
    cat "${tmp_state}" >&2 || true
    exit 1
  fi

  is_active="$(jq -r '.is_active' < "${tmp_state}")"
  is_global="$(jq -r '.is_global' < "${tmp_state}")"

  if [[ "${is_active}" != "true" ]]; then
    curl -sS -H "${auth_header}" -X POST "${WEBUI_URL}/api/v1/functions/id/${function_id}/toggle" >/dev/null
  fi

  if [[ "${is_global}" != "true" ]]; then
    curl -sS -H "${auth_header}" -X POST "${WEBUI_URL}/api/v1/functions/id/${function_id}/toggle/global" >/dev/null
  fi

  rm -f "${tmp_body}" "${tmp_get}" "${tmp_state}"
  echo "Seeded function: ${function_id}"
done
