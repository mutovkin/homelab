# truenas_reporting

Converges TrueNAS 26's `reporting.exporters` configuration over its JSON-RPC API,
so the appliance pushes its netdata metrics into the observability stack (#173).

## Why a custom module

TrueNAS 26.0 **removed the REST API** (`/api/v2.0/system/version` -> 404) and the
replacement speaks **JSON-RPC 2.0 over a WebSocket** (`/api/current` -> 400 on
GET, 405 on POST). `ansible.builtin.uri` cannot open a WebSocket, and there is no
maintained collection for the 26.0 protocol, so `library/truenas_reporting_exporter.py`
uses the official `truenas/api_client`.

That client is **not on PyPI** and reports its version as `0.0.0`, so it is pinned
by git SHA in [docs/onboarding.md](../../../docs/onboarding.md).

## Connection model

The `nas` group is `ansible_connection: local`: tasks run on the **control node**
and reach the appliance over the network. **No SSH path into the NAS is opened** —
the API credential grants exactly one role, which is a far smaller blast radius
than shell access on the machine holding all the bulk storage.

## Idempotency

The module queries, diffs, and writes **only on a real difference** (#86's
lesson — `community.proxmox` blind-PUT its full kwargs set on any mismatch and
reported `changed` forever). `attributes` is always sent **whole**: its schema
marks four fields required with no additional properties, so a partial patch
would drop them.

Only keys the playbook actually declares are compared, so an appliance-side
default the playbook is silent about can never manufacture a permanent `changed`.

The role then **re-runs the module in check mode and asserts it is clean** — the
second run inside the first. A first apply is the one run where even broken diff
logic looks like it worked.

## The two refused defaults

Both read off the live schema, both silent failures if accepted:

| Field | Appliance default | Why it is refused |
| ----- | ----------------- | ----------------- |
| `matching_charts` | `"*"` | The entire netdata firehose into a 5-year-retention TSDB on an N100 |
| `update_every` | `1` | One sample per second **per dimension** |

An exporter configured either way deploys green, stays healthy, and simply costs
storage forever — which is why these are asserts, not documentation.

## Privilege

The appliance-side account (`ansible-ctrl`) holds **`REPORTING_WRITE` only**.
Verified live: `pool.query`, `user.query`, `privilege.query` and `system.reboot`
are all denied. Granted roles are mirrored into `truenas_granted_roles` in
host_vars so the grant is reviewable in git; extend that list one line at a time
as changes need it, and do not collapse it into `FULL_ADMIN`.

Bootstrap (the one hand-configured step, since it creates the credential that
makes the rest IaC) is recorded in
[the design spec](../../../docs/superpowers/specs/2026-08-23-truenas-metrics-design.md).

## Deploy

```bash
ansible-playbook playbooks/truenas.yml --check --diff   # dry run
ansible-playbook playbooks/truenas.yml                  # apply
```

It also runs last in `site.yml`, after `deploy-services.yml` creates the telegraf
listener the exporter pushes to.
