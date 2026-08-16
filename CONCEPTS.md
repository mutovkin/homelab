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

### Monitor-only container
A service whose image updates are watched and reported but never applied
automatically, reserving the upgrade decision for an operator — used for anything
where an unattended upgrade is riskier than running slightly behind.

Being monitor-only is not the absence of opt-in: it is a second label layered on top
of the opt-in one. A container that only opts out of updating, without also opting
into scanning, is not watched at all and reports nothing — a silent-green failure of
exactly the kind this distinction exists to prevent. Scanning and updating are
separate decisions, and a container must be in scan scope before any update policy
applies to it.

### Host-only NFS bridge
A Proxmox bridge (`vmbr2`, `10.99.99.x`) carrying NFS traffic between a Docker host
and the TrueNAS guest on n5pro, isolated from the LAN so storage traffic never
touches `192.168.x.x`.

The NFS provider (TrueNAS) must be serving its export before a consumer mounts it,
and a Docker `local` NFS volume mounts only at container-create and is not retried —
so consumers must start after the provider (enforced via Proxmox guest boot order)
or self-heal once the export becomes reachable.

## Service deployment

### Service role
The single Ansible role that owns everything about one service: the Compose stack it
deploys, any configs that ship with it, and the template that renders its environment
file. One service, one role — there is no second place where a service's definition
lives.

The role is the unit of placement as well as definition: which Docker host runs a
service is decided by naming its role in that host's inventory, not by anything inside
the role.

### Deploy directory
The Ansible-owned directory on a Docker host that a service's payload is shipped to and
that Compose is then run from.

Its basename is the Compose project name, which makes the directory an identity, not
just a location: renaming it makes Compose treat the running stack as a foreign project
and recreate its containers. A repo-side rename of a service is therefore cheap, while
renaming its deploy directory is a migration — the two can and do deliberately disagree.
Because Ansible owns the directory outright, a file removed from the repo is removed
from the host, so anything living there that the repo does not know about is at risk.

### Deploy payload
The set of files shipped from a service role to its deploy directory — the Compose file
and any configs it reads from alongside it.

Distinct from a service's *data*, which lives elsewhere on the host and outlives any
deploy. The payload is disposable and reproducible; the data is neither. A relocation
that preserves the payload byte-for-byte can still disturb a service, because Compose
also derives identity from the deploy directory and behaviour from the rendered
environment file.

## Operations

### Mirror pair
Two copies of the same configuration kept in separate places by hand, where only one is
actually deployed and nothing enforces that they agree.

The dangerous property is that the inert copy is usually the more discoverable one — it
sits beside the file it appears to configure — so edits land on it and change nothing
while looking correct. A mirror pair is the most common cause of a silent-green failure
here. Collocation is the structural fix: when a service's definition has exactly one
home, there is no second copy to drift. Collocation does not, however, make a config
authoritative — the surviving copy can still be an inert mount.

### Inert mount
A config file correctly mounted into a container at the path it appears to configure,
which the application nonetheless never reads because nothing points the process at it.

Unlike a mirror pair there is no second copy to blame: the file is present, correct, and
in the right place, so every filesystem- and container-level check passes. Whether a
mount is live is an application decision rather than a filesystem fact, and a service can
honour one mounted config while ignoring its sibling in the same container. The only test
is asking the running process which file it opened — deploying a config and wiring it are
separate acts, and only the second one changes behaviour.

### Container lineage
Which tool most recently created a running container — the project's primary predictor
of whether a deploy will recreate that container or leave it alone.

Lineage is a property of the running container, not of the repo: a byte-identical
deployment can still recreate a container whose last create came from somewhere else,
because Compose matches a container against the state it expects rather than against the
file it deployed last time. Lineage is finer-grained than "Compose or not" — the Ansible
Compose module and the Compose CLI are different front-ends, and a container last acted
on by one can be recreated by the other. Attributing lineage from a container's creation
timestamp requires converting it to the host's local time first, since container metadata
is reported in UTC while update schedules are configured locally.

### Settle run
An extra deploy performed solely to absorb a known one-cycle convergence, so that the
run which follows it is a meaningful idempotency proof.

Some legitimate actions cost exactly one cycle — creating a file inside a directory the
same run synchronises, or touching a stack with the Compose CLI between two Ansible runs.
The resulting `changed` result is reconciliation, not regression, but it is only
distinguishable from a real non-idempotency bug by the next run coming back clean with
container identities unchanged. Either order the work so no such action precedes the
proof, or budget the settle run and say so before applying.

### Silent-green failure
A failure in which a control is absent or does nothing, while every signal the
project routinely checks keeps reporting success.

This is the failure shape the project treats as most expensive, because the ordinary
evidence of health — a task reporting no failures, a service reporting active, a
config file present and correct on disk — is precisely what a silent-green failure
produces. Catching one means asserting the artifact that does the work rather than
the configuration that describes it. The hardest variant is a control that never
functioned at all: it leaves no regression, no failing run, and no drift, so there is
nothing to notice and the only defence is verifying a control the first time it is
introduced.
