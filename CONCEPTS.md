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

Creation and convergence are separate concerns for a guest: it is created once from its
inventory declaration, after which only the attributes covered by a Reconcile task track
that declaration. The rest are Create-time-only fields.

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

### Rendered environment file
The per-service environment file written into the deploy directory at deploy time from
encrypted vault values, and read by Compose to resolve the stack's variable references.
It is generated on the host and never committed, so it is the one part of a service's
definition that exists nowhere in the repository.

It is a transport, and transports can corrupt. Compose's parser treats an unquoted value
as interpolatable, so a secret carrying the interpolation sigil is rewritten in passage
rather than rejected — which is why every rendered value is quoted, regardless of whether
today's secret happens to need it. A service's behaviour therefore depends on this file
being both present and faithful. Absent, only variables written with an inline fallback
take the Compose file's default; the rest resolve to empty strings, which is how a stack
comes up with an unset credential. Present but mangled, the stack starts normally on the
wrong values. Neither state fails anything — the only signal is a Compose warning on
stderr that no deploy step is gated on — so the check that matters is what the running
container actually received.

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

### Fail-open firewall
A dedicated, single-purpose packet-filter table that restricts exactly one service
port to approved sources and accepts everything else, so its worst failure mode is
the protection being absent rather than the host being unreachable.

The shape is deliberate: the table's only drop is gated on the guarded port, every
other flow exits through a terminal accept before any drop can apply, and unloading
or flushing the table reopens the port without ever blocking the host. The filter
must sit where the guarded traffic actually flows, and that differs by socket kind:
traffic to a daemon listening on the host itself is seen at input, while traffic to
a container-published port is rewritten before routing and must be filtered before
that rewrite — and then explicitly scoped to flows addressed to the host, or it
silently catches unrelated forwarded traffic. Because the pattern fails open, it
pairs with a Check-and-heal so its absence is detected rather than survived.

### Check-and-heal
A deploy-time discipline for controls whose managing service can report healthy
while the control's effect is gone: probe the real artifact on every run, treat its
absence as a change that immediately re-applies it, and finish with a hard assertion
so a still-missing control fails the run loudly.

It exists because service and task state describe past issuance, not present effect —
a unit that ran once can stay "active" long after what it created has been destroyed,
and a change-notification chain only fires when a file differs, not when the live
state does. Check-and-heal is the repair counterpart to a Silent-green failure: the
probe asks the artifact that does the work, never the configuration or service that
describes it.

### Reconcile task
An explicit task that converges one named attribute of an already-existing Guest,
written and owned by the role rather than delegated to a provisioning module.

The project reaches for these because asking a provisioning tool to ensure an object
exists says nothing reliable about what it does when the object already does: depending
on the tool and its defaults it may ignore the object entirely, or rewrite its whole
configuration. Both extremes have bitten this repo. So provisioning modules are pinned
to create-only, and every attribute that must track the inventory gets its own task
that reads live state, compares, and writes only the difference. A reconcile task is
expected to report no change once converged; one that reports a change on every run is
read as a defect rather than as noise. Attributes deliberately left out of this
treatment are Create-time-only fields.

Two traps recur. A task that mutates and then emits its own change marker reports the marker
whether or not the mutation succeeded, unless the shell aborts on error — so the marker must
be made to mean outcome rather than intent. And "converged" here means the declared value is
stored, which for a running guest may mean only a Pending guest config.

### Pending guest config
Configuration successfully written to a running Guest that the hypervisor cannot apply in
place, so it is staged and takes effect at the guest's next stop and start.

This splits convergence into two events that are easy to conflate: the write succeeded and
the declared state is stored, but the running guest is still on the old value. The
distinction matters to any Reconcile task, because the hypervisor's default config view
merges pending values in — so a task that compares against it correctly reports itself
converged and stays idempotent, while the guest has not actually received the change. Asking
specifically for the *effective* configuration, or for the pending set, is the only way to
tell the two apart. The project accepts the gap rather than restarting guests to close it,
so a staged value can remain queued indefinitely.

### Create-time-only field
A Guest attribute the role sets when it creates the guest and then never reconciles,
because converging it on a live guest would be destructive, guest-visible, or both.

The category is a deliberate boundary, not an omission: disks, mount points, network
interface definitions, firmware and machine type are declared in inventory so a guest
can be rebuilt from scratch, while the running guest is left alone. The cost is that
inventory and live state may legitimately disagree for these fields — so a declared
value is not evidence of the live value, and rebuild fidelity is a separate concern
from convergence.

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
