#!/usr/bin/env python3
"""Fail if any ansible-vault value appears in plaintext in a tracked file (#333).

The repo is public. Every vault string value (and, for hostnames/URLs, the registrable
domain inside it) is a needle; every git-tracked text file is searched. A match prints
file:line and the vault KEY — never the value — so the output is itself safe to paste.

Run via `task lint:vault-leaks` (needs .vault_password, so operator machines only).
Keys whose values are not private (a port number, a public SMTP host) are listed BY NAME
in scripts/vault-leak-allow.txt; nothing value-shaped is ever committed.
"""
import pathlib, re, subprocess, sys
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / "ansible"
MIN_LEN = 6
GENERIC_DOMAINS = {"gmail.com", "google.com", "outlook.com", "icloud.com", "github.com",
                   "openai.com", "anthropic.com", "docker.com", "example.com"}

def tracked(pattern=None):
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]

def vault_values():
    allow = set()
    af = ROOT / "scripts" / "vault-leak-allow.txt"
    if af.exists():
        allow = {l.split("#")[0].strip() for l in af.read_text().splitlines() if l.split("#")[0].strip()}
    needles = {}
    for rel in tracked():
        if not rel.endswith("vault.yml"):
            continue
        txt = subprocess.run(["ansible-vault", "view", str(ROOT / rel)], cwd=ANSIBLE,
                             capture_output=True, text=True)
        if txt.returncode != 0:
            sys.exit(f"cannot decrypt {rel} (is .vault_password present?)")
        for key, val in (yaml.safe_load(txt.stdout) or {}).items():
            if key in allow or not isinstance(val, (str, int)):
                continue
            val = str(val).strip()
            cands = {val}
            for host in re.findall(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}", val):
                labels = host.lower().split(".")
                if len(labels) >= 2:
                    cands.add(".".join(labels[-2:]))
            for c in cands:
                if len(c) >= MIN_LEN and c.lower() not in GENERIC_DOMAINS:
                    needles.setdefault(c.lower(), f"{rel}:{key}")
    return needles

def main():
    needles = vault_values()
    if not needles:
        sys.exit("no vault values loaded — refusing to report a clean tree")
    pat = re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)), re.I)
    hits = 0
    for rel in tracked():
        if rel.endswith("vault.yml"):
            continue
        try:
            text = (ROOT / rel).read_text(errors="strict")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in pat.finditer(line):
                hits += 1
                print(f"LEAK {rel}:{n}  (matches vault key {needles[m.group(0).lower()]})")
    print(f"checked {len(needles)} vault-derived needles; {hits} leak(s)")
    sys.exit(1 if hits else 0)

if __name__ == "__main__":
    main()
