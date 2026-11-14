#!/usr/bin/env python3
"""Fail if any ansible-vault value appears in plaintext in a tracked file (#333).

The repo is public. Every vault value is a needle and every git-tracked text file is
searched. A match prints file:line and the vault KEY, never the value, so the output is
always safe to paste. Nothing on any error path may echo a value either: YAML errors are
reported by file name only, and tracebacks are suppressed.

Modes:
  (default)            the WORKING TREE copy of every tracked file
  --staged             the INDEX (what a commit would contain); the pre-commit hook mode
  --message-file PATH  a commit message; the commit-msg hook mode

Exit codes: 0 clean, 1 leak found, 2 the check could not run (fail closed).

Non-private keys are allowlisted in scripts/vault-leak-allow.txt as `key` (all vault
files) or `path:key` (one file), each with a reason. An entry that matches no key is an
error, so the list cannot rot. Falsification harness: scripts/test-check-vault-leaks.sh.
"""
import os, pathlib, re, subprocess, sys

ROOT = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                   text=True).stdout.strip() or ".").resolve()
ANSIBLE = ROOT / "ansible"
MIN_LEN = 6
REQUIRED_KEYS = {"vault_base_domain"}


def die(msg, code=2):
    print(f"check-vault-leaks: {msg}", file=sys.stderr)
    sys.exit(code)


try:
    import yaml
except ImportError:
    die("PyYAML missing; run via `task lint:vault-leaks` (it uses the Ansible Python)")


class _Loader(yaml.BaseLoader):
    """Every scalar stays as its SOURCE TEXT (no int/octal/sexagesimal/bool coercion), and
    Ansible's custom tags (!unsafe, !vault, ...) load as plain values instead of raising
    an error whose message would quote the line."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node)))


def git(*args, **kw):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True, **kw).stdout


def tracked():
    return [p for p in git("ls-files", "-z").decode().split("\0") if p]


def leaves(val, key):
    """Yield (key-path, leaf-text) for every scalar inside a value, recursively."""
    if isinstance(val, dict):
        for k, v in val.items():
            yield from leaves(v, f"{key}.{k}")
    elif isinstance(val, list):
        for i, v in enumerate(val):
            yield from leaves(v, f"{key}[{i}]")
    elif isinstance(val, str):
        yield key, val
    elif val is None:
        return
    else:
        die(f"{key}: unsupported vault value type {type(val).__name__} (content withheld)")


def load_allowlist():
    path = ROOT / "scripts" / "vault-leak-allow.txt"
    if not path.exists():
        return set()
    return {l.split("#")[0].strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.split("#")[0].strip()}


def vault_needles():
    allow, used, needles, seen_keys = load_allowlist(), set(), {}, set()
    env = dict(os.environ)
    if not os.path.exists(ANSIBLE / "ansible.cfg") or "ANSIBLE_VAULT_PASSWORD_FILE" in env:
        pass
    else:  # Linked worktrees have no .vault_password of their own; use the main checkout's.
        common = pathlib.Path(git("rev-parse", "--path-format=absolute", "--git-common-dir",
                                  text=True).strip()).parent
        if not (ROOT / ".vault_password").exists() and (common / ".vault_password").exists():
            env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(common / ".vault_password")
    vault_files = [p for p in tracked() if p.endswith("vault.yml")]
    if not vault_files:
        die("no tracked vault.yml files found")
    for rel in vault_files:
        r = subprocess.run(["ansible-vault", "view", str(ROOT / rel)], cwd=ANSIBLE,
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            # ansible-vault's stderr names the cause (wrong password, not encrypted, ...)
            # and never contains plaintext.
            die(f"cannot decrypt {rel}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'unknown error'}")
        try:
            data = yaml.load(r.stdout, Loader=_Loader) or {}
        except yaml.YAMLError:
            die(f"{rel}: vault YAML does not parse (content withheld)")
        if not isinstance(data, dict):
            die(f"{rel}: vault top level is not a mapping (content withheld)")
        for key, val in data.items():
            seen_keys.update({key, f"{rel}:{key}"})
            for entry in (key, f"{rel}:{key}"):
                if entry in allow:
                    used.add(entry)
            if key in allow or f"{rel}:{key}" in allow:
                continue
            for kpath, text in leaves(val, key):
                # Multi-line values (keys, certs) are matched line by line, because the
                # search below is line by line too.
                for part in (text.splitlines() or [text]):
                    part = part.strip()
                    is_domain = key in REQUIRED_KEYS
                    if len(part) >= MIN_LEN or (is_domain and part):
                        needles.setdefault(part.casefold(), f"{rel}:{kpath}")
    missing = REQUIRED_KEYS - seen_keys
    if missing:
        die(f"required vault key(s) not found: {', '.join(sorted(missing))}")
    stale = allow - used
    if stale:
        die(f"allowlist entries match no vault key (remove them): {', '.join(sorted(stale))}")
    return needles


def sources(mode, msg_file):
    """Yield (label, text) for everything to search; unreadable inputs are errors."""
    if mode == "message":
        try:
            yield "commit message", pathlib.Path(msg_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            die(f"cannot read commit message file: {type(e).__name__}")
        return
    skipped = []
    for rel in tracked():
        if rel.endswith("vault.yml"):
            continue
        if mode == "staged":
            try:
                raw = git("show", f":{rel}")
            except subprocess.CalledProcessError:
                die(f"cannot read staged blob for {rel}")
        else:
            path = ROOT / rel
            if path.is_symlink() or path.is_dir():
                continue
            if not path.exists():
                die(f"{rel} is tracked but missing from the working tree; stage the deletion or restore it")
            raw = path.read_bytes()
        try:
            yield rel, raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(rel)
    if skipped:
        print(f"note: {len(skipped)} non-UTF-8 tracked file(s) not searched: {', '.join(skipped)}")


def main(argv):
    mode, msg_file = "worktree", None
    if argv[:1] == ["--staged"]:
        mode = "staged"
    elif argv[:1] == ["--message-file"] and len(argv) == 2:
        mode, msg_file = "message", argv[1]
    elif argv:
        die("usage: check-vault-leaks.py [--staged | --message-file PATH]")
    needles = vault_needles()
    if not needles:
        die("no vault values loaded; refusing to report a clean tree")
    order = sorted(needles, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(n) for n in order), re.IGNORECASE)
    hits = 0
    for label, text in sources(mode, msg_file):
        for n, line in enumerate(text.splitlines(), 1):
            for m in pat.finditer(line):
                hits += 1
                key = needles.get(m.group(0).casefold(), "an unidentified vault key")
                where = label if mode == "message" else f"{label}:{n}"
                print(f"LEAK {where}  (matches vault key {key})")
    print(f"checked {len(needles)} vault-derived needles ({mode}); {hits} leak(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as e:  # never let a traceback quote a decrypted line
        die(f"internal error ({type(e).__name__}); details withheld")
