#!/usr/bin/env bash
# Falsification harness for scripts/check-vault-leaks.py (#333).
#
# Builds a throwaway git repo with an ansible-vault file of FAKE values, one per leak
# category the #334 review proved the first version missed, plants each value in a
# tracked file, and requires the guard to (a) exit 1 and (b) never print the value.
# Usage: scripts/test-check-vault-leaks.sh [path/to/check-vault-leaks.py]
# Exit 0 only if every case behaves. Run against the PREVIOUS version to see it fail.
set -uo pipefail
SCRIPT=$(cd "$(dirname "${1:-$(dirname "$0")/check-vault-leaks.py}")" && pwd)/$(basename "${1:-check-vault-leaks.py}")
PY=${PY:-$(head -1 "$(command -v ansible-vault)" | sed 's/^#!//')}
export PATH="$(dirname "$PY"):$PATH"
T=$(mktemp -d "${TMPDIR:-/tmp}/vleak.XXXXXX"); trap 'rm -rf "$T"' EXIT
cd "$T" && git init -q && git config user.email t@t && git config user.name t
mkdir -p ansible/inventory/group_vars/all scripts
printf 'fake-vault-pass-for-tests\n' > .vault_password
printf '[defaults]\nvault_password_file = ../.vault_password\n' > ansible/ansible.cfg
cp "$SCRIPT" scripts/check-vault-leaks.py
cat > v.yml <<'EOF'
vault_base_domain: fakehome.co.uk
vault_plain: FakePlainSecret001
vault_unsafe: !unsafe "FakeUnsafe{{002"
vault_multi: |
  -----BEGIN FAKE KEY-----
  FakeKeyBodyLine003xyz
  -----END FAKE KEY-----
vault_nested:
  user: FakeNestedUser004
  items:
    - FakeListItem005
vault_octal: 0123456
vault_sexa: 12:34:56
EOF
ansible-vault encrypt --vault-password-file .vault_password v.yml --output ansible/inventory/group_vars/all/vault.yml >/dev/null; rm v.yml
printf 'clean\n' > README.md
git add -A && git commit -qm init
VALUES="fakehome.co.uk FakePlainSecret001 FakeUnsafe{{002 FakeKeyBodyLine003xyz FakeNestedUser004 FakeListItem005 0123456 12:34:56"
fails=0
run() { "$PY" -I scripts/check-vault-leaks.py "$@" 2>&1; }
check() { # name expected_exit output exit [required-substring]
  # The optional 5th arg is the proof the run REACHED the matcher: a leak case must name
  # the planted file, so a crash or a decrypt failure (also exit != 0) can never count
  # as a catch. That exact false green happened on this harness's first run.
  local name=$1 want=$2 out=$3 got=$4 need=${5:-} leaked=""
  for v in $VALUES; do case "$out" in *"$v"*) leaked="$leaked $v";; esac; done
  if [ "$got" != "$want" ] || [ -n "$leaked" ] || { [ -n "$need" ] && [[ "$out" != *"$need"* ]]; }; then
    fails=$((fails+1)); echo "FAIL  $name (exit $got, want $want${need:+, must mention $need}${leaked:+; PRINTED:$leaked})"
  else echo "ok    $name"; fi
}
out=$(run); check "clean tree passes" 0 "$out" $? "needles"
for v in $VALUES; do
  printf 'see %s here\n' "$v" > leak.txt; git add leak.txt
  out=$(run); check "worktree leak: ${v:0:12}…" 1 "$out" $? "leak.txt:1"
  git rm -q --cached leak.txt; rm leak.txt
done
printf 'x %s\n' FakePlainSecret001 > staged.txt; git add staged.txt; printf 'x clean\n' > staged.txt
out=$(run --staged); check "staged leak, cleaned on disk" 1 "$out" $? "staged.txt:1"
git rm -q -f --cached staged.txt; rm -f staged.txt
printf 'x %s\n' FakePlainSecret001 > gone.txt; git add gone.txt; rm gone.txt
out=$(run --staged); check "staged leak, deleted on disk" 1 "$out" $? "gone.txt:1"
git rm -q --cached gone.txt
printf 'subject\n\nbody mentions FakeNestedUser004\n' > msg.txt
out=$(run --message-file msg.txt); check "commit message leak" 1 "$out" $? "commit message"
rm msg.txt
# A vault file staged as PLAINTEXT, then encrypted only on disk, must be refused (#334 r2).
mkdir -p ansible/inventory/host_vars/zz
printf 'vault_zz: FakeStagedPlain006\n' > ansible/inventory/host_vars/zz/vault.yml
git add ansible/inventory/host_vars/zz/vault.yml
ansible-vault encrypt --vault-password-file .vault_password ansible/inventory/host_vars/zz/vault.yml >/dev/null
VALUES="$VALUES FakeStagedPlain006"
out=$(run --staged); check "plaintext vault staged, encrypted on disk" 1 "$out" $? "NOT ansible-vault encrypted"
git rm -q -f --cached ansible/inventory/host_vars/zz/vault.yml; rm -rf ansible/inventory/host_vars/zz
# Broken vault YAML must fail closed AND withhold its content.
printf 'vault_broken: "FakePlainSecret001\n' > b.yml
ansible-vault encrypt --vault-password-file .vault_password b.yml --output ansible/inventory/group_vars/all/vault.yml >/dev/null; rm b.yml
out=$(run); check "unparseable vault fails closed, prints nothing" 2 "$out" $? "does not parse"
git checkout -q -- ansible/inventory/group_vars/all/vault.yml 2>/dev/null || true
echo; [ $fails -eq 0 ] && echo "ALL CASES PASS" || echo "$fails CASE(S) FAILED"
exit $((fails > 0))
