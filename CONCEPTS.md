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

### Workbench guest
A short-lived, unprivileged Guest configured with the base OS only — no Docker, no
metrics or log agent — that an operator logs into to run command-line tools against
NAS data and then deletes.

Its lifecycle is declare, use, delete: it exists exactly as long as its inventory
declaration does, and teardown refuses to run while that declaration still stands,
because the next provisioning run would rebuild what was just destroyed. NAS access
reaches it through a Host bind mount rather than a mount of its own, since an
unprivileged guest cannot mount network filesystems. Nesting is not optional for a
workbench on a current template: the guest's own init system needs it to start its
early services, and without it the guest reports running while its network never
comes up.

### Host bind mount
A directory the Proxmox host has mounted from the NAS and hands into a Guest as a
mount point, so the guest sees the share without holding a network mount itself.

The host takes the mount, not the guest, which inverts the Host-only NFS bridge's
ordering rule: the provider is a guest of the same host, so the host can never mount
at boot and the mount must be raised on demand. What the guest binds is whatever sits
at the source path at start time — an unmounted placeholder binds as an empty
directory and stays empty for the guest's life — which is why a Bind-source start gate
guards every start. Ownership is settled on the NAS side by mapping every writer to
the share's owner, so identity inside the guest is irrelevant to what lands on disk;
the guest displays the owner as unknown, and that is expected. A bind mount allocates
nothing, so unlike an allocated mount point it is reconciled rather than
create-time-only, and must never be treated as a fresh volume awaiting restore.

### Bind-source start gate
A pre-start hook on the Proxmox host that raises the network mounts a Guest's Host
bind mounts depend on and refuses the start while any bind source is not a live,
present directory on the mounted share.

The gate exists because a start that proceeds past an absent mount does not fail — it
succeeds with an empty directory in the share's place, the quietest possible wrong
outcome. Refusing is therefore the fail-safe direction, and the gate counts as proven
only once it has been seen to refuse: with the provider unreachable, and with a
source directory missing from a mounted share.

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

### Affirmative-allow guard
A protective check that permits an operation only when its target lies inside a
positively approved set, so an unanticipated case lands on refusal rather than on
permission.

The alternative — enumerating the shapes that must be blocked and permitting
everything else — makes the accept condition a negative, and a negative accept
condition fails open by construction: it can only be as complete as its list, and
the case that matters is the one nobody thought to list. The tell that an
enumeration has been reached is repetition: each review closes exactly the hole
the last one found and the next review finds another. Inverting to an allow-list
is necessary but not sufficient. The gate must be evaluated on the target's fully
canonical form, since a positive test applied to a string the system will later
re-interpret is a negative test wearing a disguise; every error, ambiguity or
unanswerable probe must resolve to refusal; and evidence gathered from outside the
guard may widen the forbidden set but must never be what grants permission.
Finally the guard declares its threat model and names the residuals it does not
cover, because a guard with no stated scope has no definition of done and review
cannot terminate. Not to be confused with a Fail-open firewall, where "open" names
a deliberate lockout trade-off rather than a guard permitting what it forbids.

### Complement assert
An assertion that the surface a guard reads is the whole surface the deploy ships,
made by naming what the guard skipped and requiring that remainder to be empty.

A guard that parses "the files" while the deploy ships "the directory" is one
unanticipated extension away from being decorative, and the gap is invisible: the
guard still reports success over the documents it happens to match, including on the
run where the defect it exists to catch is sitting in a file it never opened.
Widening the pattern is a point fix; the complement assert is what keeps it fixed,
because it fails the first time reality grows past the guard rather than quietly
ignoring the addition. Inert residents that legitimately live in the shipped surface
are allow-listed individually with their reason, so each exemption is a recorded
decision rather than a loosened pattern. Sibling of the Affirmative-allow guard —
the same instinct, enumerate what is permitted and refuse the rest, applied to a
guard's inputs instead of its targets.

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

### Canary dry-run
A verification run that substitutes a recognisable fake value for a real secret and then
searches the run's own output for both, proving a suppression works without ever printing
the thing being protected.

The technique exists because the obvious check is circular: grepping output for a secret
and finding nothing is equally consistent with the suppression working and with the test
never having exercised the code path at all. A canary run therefore becomes evidence only
when paired with its counterfactual — the identical run against the unfixed code, which
must show both the canary and the real value leaking. Absence means something only after
presence has been demonstrated. The counterfactual's output holds a genuine secret, so it
is searched and destroyed in one step rather than displayed, and the real value is matched
by substitution so it is never echoed. Reverting a file to run the counterfactual restores
the last committed state, which silently discards an uncommitted fix — so the fix is
committed first, or re-applied and re-verified afterwards.

### Drifted fixture
A synthetic input built to carry the very condition a verification claims to surface, used
when the live system cannot be made to exhibit that condition on demand.

A fully converged fleet is the wrong instrument for any claim about drift reporting: the
branch that would demonstrate the claim is never taken, so the fixed and the unfixed code
emit identical output and the comparison establishes nothing — worse, it reads as a pass.
A fixture replaces the system's state, never its logic: the task's own expressions and
conditions run verbatim and only the side effect is stubbed, because a fixture that
paraphrases the condition tests the paraphrase. The same construction doubles as a truth
table, since once state is synthetic the awkward rows — empty values, absent values,
reversed orderings, removals — cost nothing to add. This is the mirror image of a Canary
dry-run: there a planted value proves a suppression fired, here planted state proves a
report fires.

### Shape-equivalent input
A real, achievable change that presents a guard with the same inputs its unreachable
target condition would, used to exercise it against the live system rather than a stub.

Reach for it before a Drifted fixture: enumerate what the guard actually reads, then ask
which achievable edit produces that same reading. A release-purge reconcile reads a set of
installed package names outside the pinned release, not "a release bump" — so swapping the
pinned GPU architecture drives the same selection, guards and purge through real apt and
real dpkg, on a fleet where the second release does not exist. The strength over a fixture
is that the tool runs, not one's model of it; the discipline it demands is naming where the
shapes diverge, because that residue is untested and is where defects sit — the arch swap
left the install root untouched, and the branch's worst defect lived in exactly that half.
What the substitute cannot reach is then graded honestly and written into a follow-up with
the commands to run on the day the real condition arrives, rather than left implied by a
green run.

### Source-code canary
A check that holds for every possible input by construction, kept and labelled as a
regression detector for the code that computes it rather than presented as evidence
about the data.

Such a check can only fail if someone edits the logic it mirrors, so reporting it
alongside genuine data checks overstates what a run established — a partition that
sums correctly because it is a partition proves nothing about whether the partition
is the right one. The project keeps them, because they do catch the edit they
watch, but names them in the output so a reader is not misled, and pairs them with
at least one check that derives the same quantity a second, genuinely independent
way. The discipline generalises: before trusting any check, ask what its output
would look like if the thing it guards were broken, and if the answer is "the
same", it is structural -- a regression detector, not evidence. Note the word does
not carry the sense it has in Canary dry-run, where the planted value is precisely
what makes the run evidence.

### Right-by-condition claim
A statement written together with the condition that makes it true, where the
condition is visible in the same output the statement appears in.

The project reaches for this when a claim is true of a particular run but not of
every run — a result that holds because one contributing input happened to be empty
this time, for instance. Stating it unconditionally is how a caveat becomes a false
guarantee, and stating it with a condition nobody can check is no better. So the
run prints which case it is in, from its own measured values, and the claim names
the qualifier that would reverse it. A caveat that names a condition and then does
not read it is the failure this exists to prevent.

### Window-bounded candidate
An entity a measurement could not observe within a stated window, published as a
candidate for follow-up rather than as a conclusion.

Silence is bounded below by the window and above by nothing, so absence over any
finite window cannot distinguish a dead thing from one whose normal cadence is
longer than the window — an irrigation zone that did not run today reads exactly
like a device that died. The window therefore travels with every such finding
rather than sitting in the preamble, the operator's knowledge of expected cadence
is what promotes a candidate, and a later re-run may exonerate one when the thing
finally reports. Where a measurement cannot even in principle see a class of
entity, that class is reported as inconclusive and left unranked rather than
folded in.

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

### Snapshot section
A complete copy of a Guest's configuration that the hypervisor appends to that guest's
config file when a snapshot is taken, carrying its own duplicate of every key the live
configuration holds.

The live configuration sits first in the file and each snapshot's copy follows it, which
makes the file a multi-section document wearing the costume of a flat key/value list.
Line-oriented tooling has no notion of those boundaries, so it answers questions about
*some* section rather than the live one — and a matcher that resolves to the last
occurrence will pick a snapshot's copy deterministically, not by chance. That failure is
silent in both directions: a read reports converged against a stale duplicate, and a write
edits the snapshot while leaving the running guest untouched. The hypervisor's rendered
config views never expose these sections, which is what makes them the safe way to read
state; editing the file safely means confining changes to the region ahead of the first
section header. Distinct from a Pending guest config, which is staged future state rather
than a frozen past copy, though both are sections in the same file.

### Create-time-only field
A Guest attribute the role sets when it creates the guest and then never reconciles,
because converging it on a live guest would be destructive, guest-visible, or both.

The category is a deliberate boundary, not an omission: disks, allocated mount points
(a Host bind mount is not one — it allocates nothing and is reconciled), network
interface definitions, firmware and machine type are declared in inventory so a guest
can be rebuilt from scratch, while the running guest is left alone. The cost is that
inventory and live state may legitimately disagree for these fields — so a declared
value is not evidence of the live value, and rebuild fidelity is a separate concern
from convergence.

Because a rebuild is the only thing that ever reads them, these declarations are build
orders rather than descriptions, and must be written in the form the creation path
accepts — a request to allocate a new resource — not the form that names the resource
currently in place. The two are easy to confuse because both are valid syntax for the
same field, and the one describing live state is the one that matches what the
hypervisor reports; only the other one actually builds anything. It follows that an
ordinary converged run says nothing about these fields, so their correctness cannot be
inferred from the absence of drift — the opposite of how a Reconcile task is verified.

### Frozen provisioned object
A configuration object an application imports from a declarative file once, stores its
own private copy of, and thereafter re-imports only when a counter inside that file
advances — so the file stops governing the running object without ever ceasing to
describe it.

The trap is that the file stays valid, stays deployed, and stays the obvious source of
truth, while every later edit to it — a changed endpoint, a rotated credential — has no
effect for the object's whole life. It is the same boundary a Create-time-only field
draws, inverted in the way that matters: a create-time-only field is a boundary the role
declares and an operator can read in inventory, whereas this one lives inside the
application and the deployment pipeline knows nothing about it. Content-addressed
delivery cannot detect it either, because the file genuinely did change and genuinely
did arrive.

Two rules follow. The counter is part of every edit rather than metadata attached to one,
so a change that does not advance it is incomplete by construction. And the only check
that observes the object's real state is one that makes the application exercise it
against its dependency using the stored copy — proving the process is alive, or that the
file is present and correct on disk, proves nothing here. That makes it a Silent-green
failure by construction, and the compensating control belongs in the deploy rather than
in the monitoring, because monitoring that queries through the frozen object shares its
fault and cannot report it.

### All-or-nothing provisioning
An import of declarative configuration that an application validates as a single
document at start-up, so that one rejected entry withdraws every entry in the set —
and, where the importer is a start-up dependency, prevents the application from
serving at all.

It is the opposite boundary from a Frozen provisioned object: there the file stops
governing a running object, here the file governs so completely that a defect in one
line of it removes the whole capability. The consequence worth planning around is that
blast radius does not track the size of the mistake. A single mistyped annotation on a
single rule is indistinguishable, in effect, from deleting the directory. Where the
withdrawn capability is the alerting stack itself, the failure also removes the thing
that would have reported it.

The failure is loud at the process level and mute about its cause: the only statement
of which entry was rejected is in the application's own log, so an operator sees a
service that will not start and no indication that a configuration file is why. It is
not a Silent-green failure — nothing claims success — but it defeats the same class of
checks, because a document can be well-formed in the deploying repository's terms and
still be rejected by the application's schema, which no repository-side parser or lint
knows. Two rules follow. A deploy that installs such a document must gate on the
application actually serving afterwards, since the installing step reports on writing
the file rather than on the application accepting it. And a dry run is not a test:
the step that would have exposed the defect is precisely the one a dry run skips.

### Inventory-name label
The identity a per-host signal is tagged with, which is by convention the name the
inventory uses for that host and not any name the host can discover about itself.

A machine knows several names — its OS hostname, the id of whatever container the
collector happens to be running in, whatever DNS says — and a collector asked for
"the hostname" will return one of those. None of them is the name an alert or a
playbook is written against, and two of them change without anyone editing anything.
Because the correct value cannot be derived on the host, it is injected from the
deploy layer and made mandatory there: a missing value must abort the deployment
rather than resolve to an empty label, since a signal labelled wrongly-but-plausibly
is harder to notice than one that is missing. Mandatory has to mean mandatory at
every hop — a collector handed a placeholder it cannot resolve does not necessarily
refuse to start; it may emit the unresolved placeholder as the label and stay
healthy, which is the wrongly-but-plausibly case arriving by a different door.

Injecting the value is also not the end of the job, because a collector can strip
the label back off after stamping it. Where a collector offers a tag allowlist, that
allowlist filters the finished signal rather than choosing which raw fields become
tags, so it silently removes the injected identity along with any constants the
collector attaches to everything it emits. Those constants are the cheap tell: if
they are missing too, the identity was stamped and then filtered, not never stamped.

### Structured log stream
A second, machine-parseable copy of a host's system log, emitted alongside the
human-facing log files rather than replacing them, because the default file format
omits fields the collector needs.

It exists for the case where data is not merely hard to extract but absent: the
routine format drops the priority that carries severity and facility, so no parser
can recover them and the collector is reduced to asserting a single hardcoded level
for every record. The human-facing files are deliberately left in their original
format — they are what an operator reads during an incident — so the two streams
carry the same messages under different formats, and the collector must read exactly
one of them or every event is stored twice. Because the stream is a duplicate, its
growth is bounded on the same rotation cadence as the original.

### Absence-owning rule
Among several alert rules watching the same signal, the single rule designated to
fire when the signal stops arriving at all, so that every other rule treats
no-data as healthy.

Absence and badness are different questions and they are usually answered by
different queries: a threshold rule sees nothing when a collector dies, and a rule
written to detect "nothing" must ask a question that still returns rows for a source
that has gone quiet. Without a designated owner, either every rule treats no-data as
an alert — so one dead collector pages once per rule — or every rule treats it as
healthy, and a dead collector is indistinguishable from a healthy system. A separate
failure mode the owner does not cover is a source that never started: absence
measured against a signal that has never existed returns nothing and is
indistinguishable from a mistyped query, so that case has to be watched at the
receiving end instead, on a counter maintained there rather than by the sender.

A third failure mode belongs to the owner's query shape rather than its signal, and
it appears only when an identity is deliberately renamed. An absence rule written to
evaluate each identity separately measures staleness per series, and a series that
has stopped receiving samples remains inside the rule's lookback window until it ages
out — so the abandoned identity keeps a growing staleness value and the rule fires
for the whole width of that window. This is tolerable when the rename predates the
rule; when the rule ships in the same change as the rename, it arrives firing. Two
query shapes resolve it, and they differ in which direction they fail. The rule can
stay identity-agnostic and exclude the retired identity by name, which quiets it
immediately and needs no further attention once that identity ages out of the
lookback — but the exclusion is then dead weight, and a wrong-but-present identity
is once again indistinguishable from a healthy one, so the rule has gone quiet
about the very thing it was written to catch. Or the rule can be scoped to the
identity it expects, so that a wrong identity ages the query down to no rows at
all, which such a rule is configured to treat as an alert. The scoped form is
preferred because its failure direction is a page rather than silence: it fails
loud, and stays loud until someone corrects the name. Its cost is the mirror image
— it watches exactly the identities it lists, and a newly added one is unwatched
until it is listed too.

The owner's own signal is the part most easily got wrong, because choosing it feels
like naming rather than measuring. A signal can only carry an absence rule if it is
continuously present while the system is healthy, and that is a property to be
sampled, not assumed — a receiving-end counter is not automatically continuous, since
counters are commonly published only once non-zero and some only while activity is
in flight. Where the signal of interest is intermittent, the choices are to
manufacture a continuous one or to watch a continuously published stand-in on the
same collection path; the stand-in then has to be recorded as load-bearing at both
ends, because nothing connects a rule to the collection filter that feeds it.

### Paired deep link
The reserved pair of alert annotations that turns a notification into a one-tap jump
to the graph the alert is about: a dashboard identifier and a panel identifier, which
the dashboard platform validates only as a pair.

Half a pair is not a degraded link but a rejected document, and the rejection is not
scoped to the offending rule — provisioning is a startup-blocking stage, so one
unpaired annotation stops the whole process from serving and withdraws every
provisioned rule in the org. No "link at the dashboard, no panel" form is
representable, so even a rule whose subject is an entire delivery path going dark has
to name one panel; the convention is to pick the panel that goes blank when the rule
fires — or, for a rule about a signal that is lying rather than missing, the panel
where the flat line is legible — and to write that reasoning beside the pair so a
later cleanup pass does not tidy the redundant-looking line away. The same pair is
the precondition for an attached screenshot: a rule with no panel association can
never carry one, whatever the renderer situation is.

### Linkable panel id
A dashboard panel identifier that is a legal deep-link target, as distinct from one
that merely exists and is pinned.

Structural elements — rows above all — take identifiers from the same sequence as
panels and consume them, but a link at one renders nothing while still showing its
button, so the mistake is silent at every layer and sits one keystroke from a real
target. Two sets therefore have to be kept apart: the linkable set, which excludes
structural elements, and the full set, which includes them because the "every panel
is pinned" check is only meaningful over all of them. Pinning matters because
unpinned identifiers are assigned at load, making them an accident of document order
that an inserted panel or a UI re-export silently renumbers; pinned identifiers must
also be unique, since duplicates satisfy a naive count while leaving the target
ambiguous.

### Heartbeat marker
A signal emitted on a fixed cadence for no reason other than to be counted, so that
its absence is unambiguous.

It exists because most real signals are bursty: a source can be silent for a long
stretch in perfect health, which makes "nothing arrived" and "nothing was supposed to
arrive" the same observation, and any rule written on the raw stream is either
constantly wrong or permanently useless. A marker gives the healthy state a guaranteed
floor, so a count falling below it means the path is broken rather than the source
being quiet — this repairs the obvious rule instead of replacing it, which is why the
rule is usually kept and the marker added underneath. A marker only proves the segment
it actually traverses, so it is emitted at the far end of the path being watched, not
next to the thing doing the watching. The same shape applies to a delivery path, where
the marker is a notification that is meant to arrive on a schedule and whose silence is
the alarm; there the receiving human is the detector, and nothing about the alerting
system needs to be working for the absence to be noticed.

### Sentinel signal
A continuously published signal watched as a stand-in for one that matters but is not
continuous enough to carry an Absence-owning rule.

The substitution is only valid if the stand-in travels the identical collection path,
because the claim being made is about that path rather than about the stand-in itself.
Its value is often a second, free signal — a stand-in chosen from the same subsystem
usually encodes something about that subsystem's configuration. The cost is a coupling
nothing enforces: the rule depends on the stand-in continuing to be collected, and a
collection filter that no longer matches it disables the rule without any error, which
is the failure the rule was there to catch. That coupling therefore has to be recorded
in both the rule and the collector, since a note in only one of them is one edit away
from being false.

### Blank-credential disable
A credential whose empty value is read by the consuming software as an instruction to
switch the protection off, rather than as a malformed value to reject.

It turns an ordinary configuration mistake — a renamed, misspelled or not-yet-set secret —
into a running system with the protection removed and every health signal green, giving it
an unusually short path from typo to exposure among Silent-green failures. The defence is
to refuse blankness at each layer that could introduce it rather than at the one that
consumes it: assert the value is present before any state is touched, forbid an empty
fallback where it is rendered, and make the stack refuse to start on an unset value. A
health endpoint does not test for this, because services commonly exempt those endpoints
from the very control in question; the test is a deliberately unauthenticated request that
must be refused.

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

A further variant survives every check made on the control itself. The control runs,
its logic is sound and its extraction is deliberately non-vacuous — and it still
cannot see the fault, because the field it asserts on cannot represent that fault.
This arises wherever the asserted value is derived downstream of a configurable
mapping, a setting of the form "when this happens, report that instead": such a field
describes the operator's declared preference rather than the observed world, and a
fault the mapping absorbs never reaches it. Reviewing the control cannot surface
this, because nothing about the control is wrong. Only running it against the fault
does. A control that passes its own negative test has reported a fact about itself
rather than about the system, so the question to ask of every asserted field is what
value it would hold if the fault were present — if that is the value it already
holds, the control is decoration however carefully the rest of it is built.

### Pre-deploy dump
A backup a service role takes of its own data immediately before handing that data to a
possibly newer image, so a one-way schema migration always has something to go back to.

Gated on the DATA existing, never on an upgrade being pending: the dump is cheap and
online, while a wrong gate computation means no backup at all in front of an
irreversible migration. Fail-safe over-backup is the deliberate trade, which is why a
routine redeploy legitimately reports work done here and why an idempotency check must
not chase this task to zero. Scope is the data the migration can destroy — not
everything that happens to share the same database server.

### Restore drill
An exercise that proves a backup by restoring it under a scratch identity and comparing
object counts against the live source, then discarding the copy.

Restoring is not the drill; *counting* is. A restore that aborts partway still leaves a
database that exists, carries the right owner, and holds nothing — so identity-level
checks are guaranteed to pass and therefore carry no information. The drill is also
version-coupled: it reads the backup tool's output format, so a server major-version
upgrade can silently turn it into a ritual that proves nothing. Re-run it after such an
upgrade, not only after the backup code changes. The written recipe is part of what is
under test, not instructions for testing something else — so the drill exercises the
command exactly as it is published, composed and end to end, rather than a retyped
approximation of it.

### Carried verification
Checking an artifact's content *before* a transform such as compression or encoding, then
carrying that check across the transform with an integrity primitive the transform's own
format provides, rather than re-checking the transformed artifact.

The ordering exists because content checks here are deliberately pipeline-free — reading a
bounded region into a variable and testing it — and re-checking a transformed artifact
requires piping it back through a decoder, which is a different check with different failure
modes. A stored checksum over the pre-transform bytes closes the gap without reintroducing
that pipeline: if it validates, the transformed artifact decodes to exactly the bytes already
vouched for. The trade is that the intermediate and the transformed artifact briefly coexist,
which is accepted where scratch space is not the binding constraint; the streaming
alternative that removes it is precisely what would force the checks back into a pipeline.
