#!/usr/bin/env bash
#
# Validate every Docker Compose stack under containers/ without a Docker daemon.
#
# The real `.env` files are templated onto the hosts at deploy time from
# ansible-vault and are never committed, so `docker compose config` would fail on
# unset-variable interpolation.  We therefore synthesise a throwaway `.env` per
# stack containing every `${VAR}` / `${VAR:-default}` name referenced by the
# compose file, then run `config -q`, which parses, interpolates, merges and
# schema-validates the file — entirely client-side.
#
# Usage: scripts/validate-compose.sh [compose-file ...]
#        (no arguments = all containers/*/*.yml)

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Compose CLI: prefer the `docker compose` plugin, fall back to a standalone
# `docker-compose` binary (Homebrew installs the plugin outside Docker's search
# path on machines without Docker Desktop).
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
elif [[ -x /opt/homebrew/lib/docker/cli-plugins/docker-compose ]]; then
  compose=(/opt/homebrew/lib/docker/cli-plugins/docker-compose)
else
  echo "error: no docker compose CLI found" >&2
  exit 1
fi

files=()
if [[ $# -gt 0 ]]; then
  files=("$@")
else
  # `while read` rather than `mapfile`, which needs bash 4 (macOS ships bash 3.2).
  while IFS= read -r found; do
    files+=("${found}")
  done < <(find "${repo_root}/containers" -mindepth 2 -maxdepth 2 -name '*.yml' | sort)
fi

if [[ ${#files[@]} -eq 0 ]]; then
  echo "error: no compose files found" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

# Pick a syntactically plausible dummy value per variable name.  Compose only
# validates a handful of fields structurally (CIDRs, ports, memory sizes), but
# feeding it garbage there would produce failures that say nothing about the file.
dummy_value() {
  case "$1" in
    *SUBNET*) echo "172.31.255.0/24" ;;
    *_PORT | *_PORT_* | *PORT) echo "8080" ;;
    *MEMORY_LIMIT* | *_LIMIT) echo "512m" ;;
    *_GID | *_UID) echo "1000" ;;
    *_DIR | *_PATH | *LOCATION*) echo "/tmp/ci-compose-validation" ;;
    TIMEZONE | *_TIMEZONE | TZ) echo "UTC" ;;
    *) echo "dummy" ;;
  esac
}

failures=0
for file in "${files[@]}"; do
  name="$(basename "$(dirname "${file}")")"
  env_file="${tmpdir}/${name}.env"
  : >"${env_file}"

  # Every `${NAME`, `${NAME:-…}`, `${NAME-…}` reference in the file.  Matches inside
  # comments are harmless: an unused variable in the env file changes nothing.
  while read -r var; do
    [[ -n "${var}" ]] || continue
    printf '%s=%s\n' "${var}" "$(dummy_value "${var}")" >>"${env_file}"
  done < <(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*' "${file}" | cut -c3- | sort -u)

  if "${compose[@]}" -f "${file}" --env-file "${env_file}" config -q; then
    echo "ok       ${file#"${repo_root}/"}"
  else
    echo "FAILED   ${file#"${repo_root}/"}" >&2
    failures=$((failures + 1))
  fi
done

if [[ ${failures} -gt 0 ]]; then
  echo "${failures} compose file(s) failed validation" >&2
  exit 1
fi

echo "all ${#files[@]} compose file(s) valid"
