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
  --pre-push REMOTE    every commit a push would publish (message + ADDED lines), read
                       from git's pre-push stdin; the last gate before GitHub. Catches
                       commits that skipped the other hooks (--no-verify, rebases,
                       cherry-picks, other machines).

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
    # The installed hooks run a SNAPSHOT of this script from the hooks dir (independent of
    # whatever branch is checked out) and point this at the allowlist snapshot beside it.
    path = pathlib.Path(os.environ.get("VAULT_LEAK_ALLOW") or ROOT / "scripts" / "vault-leak-allow.txt")
    if not path.exists():
        return set()
    return {l.split("#")[0].strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.split("#")[0].strip()}


def vault_needles(mode):
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
        if mode == "staged":
            # Read the vault file from the INDEX, i.e. what the commit will contain (#334
            # review r2: a plaintext vault.yml staged, then encrypted only on disk, passed
            # when this decrypted the working-tree copy). Plaintext in the index is the
            # worst leak this guard exists for, so it is an error on sight.
            try:
                blob = git("show", f":{rel}")
            except subprocess.CalledProcessError:
                die(f"cannot read staged {rel}")
            if not blob.lstrip().startswith(b"$ANSIBLE_VAULT;"):
                die(f"staged {rel} is NOT ansible-vault encrypted; refusing (content withheld). "
                    f"Encrypt it and `git add` it again", code=1)
            r = subprocess.run(["ansible-vault", "decrypt", "--output=-", "-"], cwd=ANSIBLE,
                               input=blob.decode("utf-8", "replace"), capture_output=True,
                               text=True, env=env)
        else:
            r = subprocess.run(["ansible-vault", "view", str(ROOT / rel)], cwd=ANSIBLE,
                               capture_output=True, text=True, env=env)
        if r.returncode != 0:
            # ansible-vault's stderr names the cause (wrong password, not encrypted, ...)
            # and never contains plaintext. Its LAST line is usually `Origin: <path>`, so
            # report the line after `<<< caused by >>>`, else the first [ERROR] line.
            lines = [l.strip() for l in r.stderr.splitlines() if l.strip()]
            cause = next((lines[i + 1] for i, l in enumerate(lines[:-1]) if "caused by" in l),
                         next((l for l in lines if "ERROR" in l), "unknown error"))
            die(f"cannot decrypt {rel}: {cause}")
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
                    # PEM armor and shebangs are boilerplate, not secret; as needles they
                    # would flag every doc that shows a key header (review r2 #3).
                    if re.match(r"^-----(BEGIN|END) [A-Z0-9 ]+-----$", part) or part.startswith("#!"):
                        continue
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


def push_revs(remote):
    """Rev arguments for each ref git is about to push (pre-push hook stdin)."""
    out = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            die(f"unexpected pre-push input line ({len(parts)} fields)")
        _lref, lsha, _rref, rsha = parts
        if set(lsha) == {"0"}:
            continue  # deleting a remote ref publishes nothing
        known = set(rsha) != {"0"} and subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", rsha + "^{commit}"],
            capture_output=True).returncode == 0
        # New ref, or a force-push over a commit we do not have: check every commit the
        # remote does not already have (over-inclusive is safe; under-inclusive is not).
        out.append([f"{rsha}..{lsha}"] if known else [lsha, "--not", f"--remotes={remote}"])
    return out


def push_sources(remote):
    """Yield (label, text) per commit: its message, and each file's ADDED lines."""
    for revs in push_revs(remote):
        try:
            raw = git("log", "-p", "--text", "--no-color", "--no-ext-diff", "--format=%x00COMMIT %H%n%B%x00ENDMSG", *revs)
        except subprocess.CalledProcessError:
            die("cannot list the commits being pushed; refusing")
        text = raw.decode("utf-8", "replace")
        for chunk in text.split("\x00COMMIT ")[1:]:
            sha, _, rest = chunk.partition("\n")
            msg, _, patch = rest.partition("\x00ENDMSG")
            yield f"commit {sha[:12]} message", msg
            current, added = None, []
            for line in patch.splitlines():
                if line.startswith("diff --git "):
                    if current and added:
                        yield f"commit {sha[:12]} {current}", "\n".join(added)
                    current, added = line.split(" b/", 1)[-1], []
                elif line.startswith("+") and not line.startswith("+++"):
                    added.append(line[1:])
            if current and added:
                yield f"commit {sha[:12]} {current}", "\n".join(added)


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
    elif argv[:1] == ["--pre-push"] and len(argv) == 2:
        mode, msg_file = "push", argv[1]
    elif argv:
        die("usage: check-vault-leaks.py [--staged | --message-file PATH | --pre-push REMOTE]")
    # A push is checked against the vault as it stands in the working tree.
    needles = vault_needles("worktree" if mode == "push" else mode)
    if not needles:
        die("no vault values loaded; refusing to report a clean tree")
    order = sorted(needles, key=len, reverse=True)
    # Needles are case-folded, so the searched text is folded too (re.IGNORECASE alone
    # would miss e.g. a value with 'ß', whose folded form is 'ss'). Only line numbers are
    # reported, so matching on the folded copy loses nothing.
    pat = re.compile("|".join(re.escape(n) for n in order))
    hits = 0
    srcs = push_sources(msg_file) if mode == "push" else sources(mode, msg_file)
    for label, text in srcs:
        for n, line in enumerate(text.splitlines(), 1):
            for m in pat.finditer(line.casefold()):
                hits += 1
                key = needles.get(m.group(0).casefold(), "an unidentified vault key")
                where = label if mode in ("message", "push") else f"{label}:{n}"
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
