# Concepts

Shared domain vocabulary for this project — entities, named processes, and status
concepts with project-specific meaning. Seeded with core domain vocabulary, then
accretes as ce-compound and ce-compound-refresh process learnings; direct edits are
fine. Glossary only, not a spec or catch-all.

## Infrastructure

### Control machine
An operator's laptop that drives the homelab by running Ansible — currently a Mac
and an Arch/Omarchy PC.
*Avoid:* operator machine

Deploys originate here, not on the servers: VM/LXC provisioning tasks run with
`delegate_to: localhost`, so the control machine's own Python makes the Proxmox API
calls. Its local environment (network permissions, installed Python deps) can
therefore break a deploy even when every server is healthy.

### Proxmox host
A physical machine running Proxmox VE that hosts VMs and LXC containers — the two
are `eq12` (node `pve`) and `n5pro` (node `n5pro`).

### Guest
A VM or LXC container provisioned on a Proxmox host by the `proxmox_guests` role.
Guests are the unit the host layer creates and the service layer then configures.

### Docker host
A guest (always a privileged LXC here — `deb-docker` on eq12, `n5pro-docker` on
n5pro) that runs Docker and the project's Compose service stacks.

Because Docker runs inside a privileged LXC rather than on bare metal, it cannot
manage host-only kernel facilities: every Compose service must opt out of AppArmor
(`security_opt: apparmor:unconfined`) and the LXC's own AppArmor service is masked
before Docker starts.
