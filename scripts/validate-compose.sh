#!/usr/bin/env bash
#
# Validate every Docker Compose stack under ansible/roles/services/*/files/ without a Docker daemon.
#
# Two checks per stack:
#
#   1. `docker compose config -q` — parses, interpolates, merges and
#      schema-validates the file.  The stacks live at
#      roles/services/<svc>/files/compose.yaml.  The real `.env` files are
#      templated onto the hosts at deploy time from ansible-vault and are never
#      committed.  Without one, compose does not error: it warns and interpolates
#      every variable to a blank string, and 7 of the 11 stacks still exit 0 — so
#      the run would be testing the missing environment rather than the file.  We
#      therefore synthesise a throwaway `.env` covering every `${VAR}` the file
#      names.
#
#   2. env-template cross-check — every `${VAR}` referenced *without* a
#      `:-default` must actually be emitted by the Ansible role that deploys the
#      stack (the sibling templates/env.j2 in the same role, which becomes the
#      stack's .env).  Both required forms count: the bare `${VAR}` and the
#      hard-fail `${VAR:?message}` (#88).  A `:?` reference is the strongest claim
#      a compose file can make that a variable is mandatory, so it needs this
#      rename protection more than a bare reference, not less.  Check 1 alone
#      cannot catch a misspelled variable, because the dummy env is derived from
#      the compose file itself and so agrees with any typo; this check compares it
#      against the only real source of values.
#
# Scope limit worth knowing before you trust a green run: this validates the
# compose files themselves.  It does NOT check that bind-mounted host paths exist
# or that the role actually deploys the config they point at — the failure behind
# the Vector 0.57 incident (see CLAUDE.md, "Bind-mounted config must be owned by
# Ansible").  A green check here is not evidence the stack will start.
#
# Usage: scripts/validate-compose.sh [compose-file ...]
#        (no arguments = every roles/services/<svc>/files/compose.yaml)

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Compose CLI, in order: 1) the `docker compose` plugin; 2) a standalone
# `docker-compose` on PATH; 3) Homebrew's plugin binary invoked directly, which
# `docker` cannot find on machines without Docker Desktop unless
# `cliPluginsExtraDirs` is set — both Homebrew prefixes, since Apple Silicon and
# Intel macOS differ.
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
elif [[ -x /opt/homebrew/lib/docker/cli-plugins/docker-compose ]]; then
  compose=(/opt/homebrew/lib/docker/cli-plugins/docker-compose) # Apple Silicon
elif [[ -x /usr/local/lib/docker/cli-plugins/docker-compose ]]; then
  compose=(/usr/local/lib/docker/cli-plugins/docker-compose) # Intel macOS
else
  echo "error: no docker compose CLI found" >&2
  exit 1
fi

files=()
if [[ $# -gt 0 ]]; then
  files=("$@")
else
  # One stack per service role: roles/services/<svc>/files/compose.yaml (#85).
  # Roles starting with `_` are shared helpers (services/_deploy) and ship no
  # stack. Any OTHER role missing its compose file is a hard error rather than a
  # silent skip — under-covering is the failure mode that matters for a
  # validation gate.
  for dir in "${repo_root}"/ansible/roles/services/*/; do
    svc="$(basename "${dir}")"
    case "${svc}" in _*) continue ;; esac
    if [[ -f "${dir}files/compose.yaml" ]]; then
      files+=("${dir}files/compose.yaml")
    else
      echo "error: ${dir} has no files/compose.yaml compose file" >&2
      exit 1
    fi
  done
fi

if [[ ${#files[@]} -eq 0 ]]; then
  echo "error: no compose files found" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

# Pick a syntactically plausible dummy value per variable name, so a failure means
# something about the file rather than about our placeholder.  Measured against
# these stacks, only two categories actually bite: bind-mount specs (a blank
# `${..._DIR}` gives `invalid spec: :/path`) and `deploy.resources.limits.memory`
# (rejects anything that isn't a size).  The subnet and port arms are belt and
# braces — compose does not currently validate either — but they cost nothing and
# keep the dummy env readable.
dummy_value() {
  case "$1" in
  *SUBNET*) echo "172.31.255.0/24" ;;
  *MEMORY_LIMIT* | *_LIMIT) echo "512m" ;;
  *PORT) echo "8080" ;;
  *_GID | *_UID) echo "1000" ;;
  *_DIR | *_PATH | *LOCATION*) echo "/tmp/ci-compose-validation" ;;
  *TIMEZONE | TZ) echo "UTC" ;;
  *) echo "dummy" ;;
  esac
}

failures=0
for file in "${files[@]}"; do
  rel="${file#"${repo_root}/"}"
  svc="$(basename "$(dirname "$(dirname "${file}")")")"   # …/services/<svc>/files/compose.yaml
  env_file="${tmpdir}/${svc}.env"
  : >"${env_file}"

  # Compose also honours a bare `$NAME`, which this script's `${NAME}` matching
  # would miss — and a missed variable interpolates to blank and still passes
  # `config -q`. Rather than document that as a convention, enforce it.
  bare="$(grep -vE '^[[:space:]]*#' "${file}" |
    grep -oE '(^|[^$])\$[A-Za-z_][A-Za-z0-9_]*' | grep -oE '\$[A-Za-z_][A-Za-z0-9_]*' |
    sort -u | tr '\n' ' ' || true)"
  if [[ -n "${bare}" ]]; then
    echo "FAILED   ${rel} (bare \$NAME interpolation — use \${NAME}: ${bare})" >&2
    failures=$((failures + 1))
    continue
  fi

  # Every `${NAME`, `${NAME:-…}`, `${NAME-…}` reference.  Matches inside comments
  # are harmless: an unused variable in the env file changes nothing.
  while read -r var; do
    [[ -n "${var}" ]] || continue
    printf '%s=%s\n' "${var}" "$(dummy_value "${var}")" >>"${env_file}"
  done < <(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*' "${file}" | cut -c3- | sort -u || true)

  # Capture stderr: compose reports an unresolved variable as a *warning* and still
  # exits 0, so the exit code alone would let a blank interpolation through.
  if ! config_out="$("${compose[@]}" -f "${file}" --env-file "${env_file}" config -q 2>&1)"; then
    printf '%s\n' "${config_out}" >&2
    echo "FAILED   ${rel} (compose config)" >&2
    failures=$((failures + 1))
    continue
  fi
  if printf '%s' "${config_out}" | grep -q 'variable is not set'; then
    printf '%s\n' "${config_out}" >&2
    echo "FAILED   ${rel} (unresolved variable)" >&2
    failures=$((failures + 1))
    continue
  fi

  # Cross-check against the env template.  Two forms are required and therefore
  # checked: bare `${NAME}`, and `${NAME:?msg}` which aborts the whole parse when
  # unset (#88).  `${NAME:-default}` is skipped — self-sufficient by construction.
  # Comment lines are stripped so a `${VAR}` mentioned in prose doesn't count.
  # The env template is the sibling templates/env.j2 of the role that owns this
  # compose file — the layout guarantees the pairing (#85).
  template="${repo_root}/ansible/roles/services/${svc}/templates/env.j2"
  if [[ ! -f "${template}" ]]; then
    echo "FAILED   ${rel} (no env.j2 found for service '${svc}')" >&2
    failures=$((failures + 1))
    continue
  fi

  # `grep '^NAME='` is blind to Jinja control flow: a variable emitted only inside
  # an `{% if %}` would count as always-defined.  No template uses control flow
  # today; say so loudly if one starts, so the gap never goes unnoticed.
  if grep -q '{%' "${template}"; then
    echo "warning  ${template#"${repo_root}/"} uses Jinja control flow — the" \
      "cross-check below treats conditionally-emitted variables as defined" >&2
  fi

  # LC_ALL=C so sort/comm agree on ordering regardless of the caller's locale —
  # a mismatch makes comm print to stderr and still exit 0, i.e. fail silently.
  referenced="$(grep -vE '^[[:space:]]*#' "${file}" |
    grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*(\}|:\?)' |
    sed -E 's/^\$\{//; s/(\}|:\?)$//' | LC_ALL=C sort -u || true)"
  defined="$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "${template}" | tr -d '=' | LC_ALL=C sort -u || true)"
  undefined="$(LC_ALL=C comm -23 <(printf '%s\n' "${referenced}") <(printf '%s\n' "${defined}") |
    grep -v '^$' | tr '\n' ' ' || true)"

  if [[ -n "${undefined}" ]]; then
    echo "FAILED   ${rel} (not emitted by ${template#"${repo_root}/"}: ${undefined})" >&2
    failures=$((failures + 1))
    continue
  fi

  echo "ok       ${rel}"
done

if [[ ${failures} -gt 0 ]]; then
  echo "${failures} compose file(s) failed validation" >&2
  exit 1
fi

echo "all ${#files[@]} compose file(s) valid"
