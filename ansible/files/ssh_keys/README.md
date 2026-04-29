# SSH Public Keys

Drop your `*.pub` files here. The `common` Ansible role automatically provisions
all keys in this directory to every managed host (Proxmox hosts + Docker LXCs).

## Naming Convention

`<user>@<hostname>.pub` — e.g., `surge@macbook.pub`, `surge@omarchy.pub`

## Adding a key

```bash
cp ~/.ssh/id_ed25519.pub ansible/files/ssh_keys/$(whoami)@$(hostname).pub
```

Keys are deployed on the next `task deploy:full` or `task infra:hosts` run.
