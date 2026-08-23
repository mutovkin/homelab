#!/usr/bin/python
"""Converge a TrueNAS 26 `reporting.exporters` entry over the JSON-RPC API.

Why a custom module and not `ansible.builtin.uri`: TrueNAS removed the REST API
in 26.0 (measured: `/api/v2.0/system/version` -> 404) and the replacement speaks
JSON-RPC 2.0 over a WebSocket (`/api/current` -> 400 on GET, 405 on POST). `uri`
cannot open a WebSocket, so there is no stock-module path to this appliance.

Idempotency contract, shaped by #86: this module QUERIES first, compares against
the desired state, and issues create/update ONLY on a real difference. It never
blind-PUTs. `community.proxmox` taught the repo what the other behaviour costs —
a module that pushes its full kwargs set on any mismatch reports `changed` every
run and eventually writes something nobody asked for.

`attributes` is always sent WHOLE on update. Its schema (read from the live box)
marks exporter_type/destination_ip/destination_port/namespace required with no
additional properties, so a "send only what changed" patch would drop required
subfields. Declare every attribute in host_vars and this stays total.
"""

from ansible.module_utils.basic import AnsibleModule

TRUENAS_IMPORT_ERROR = None
try:
    from truenas_api_client import Client
except ImportError as exc:  # pragma: no cover - reported to the operator below
    Client = None
    TRUENAS_IMPORT_ERROR = str(exc)


def diff_exporter(current, desired):
    """Return {field: (old, new)} for every field that must change.

    Only keys present in `desired` are compared: attributes the playbook does
    not declare are left to whatever the appliance already holds, so an
    undeclared default can never manufacture a permanent `changed`.
    """
    changes = {}
    for key in ("name", "enabled"):
        if key in desired and current.get(key) != desired[key]:
            changes[key] = (current.get(key), desired[key])

    cur_attrs = current.get("attributes") or {}
    for key, want in (desired.get("attributes") or {}).items():
        if cur_attrs.get(key) != want:
            changes[f"attributes.{key}"] = (cur_attrs.get(key), want)
    return changes


def main():
    module = AnsibleModule(
        argument_spec=dict(
            api_url=dict(type="str", required=True),
            api_key=dict(type="str", required=True, no_log=True),
            validate_certs=dict(type="bool", default=False),
            name=dict(type="str", required=True),
            enabled=dict(type="bool", default=True),
            attributes=dict(type="dict", required=True),
        ),
        supports_check_mode=True,
    )

    if Client is None:
        module.fail_json(
            msg=(
                "The truenas_api_client Python package is missing on the CONTROL "
                "NODE. It is not on PyPI and installs only from git; add it to the "
                "ansible tool environment (see docs/onboarding.md): "
                "uv tool install ansible-core --with ansible --with proxmoxer "
                "--with requests --with 'truenas-api-client @ "
                "git+https://github.com/truenas/api_client.git@<pinned-sha>'. "
                "Import error: %s" % TRUENAS_IMPORT_ERROR
            )
        )

    p = module.params
    desired = {
        "name": p["name"],
        "enabled": p["enabled"],
        "attributes": p["attributes"],
    }

    try:
        with Client(p["api_url"], verify_ssl=p["validate_certs"]) as client:
            if not client.call("auth.login_with_api_key", p["api_key"]):
                module.fail_json(
                    msg=(
                        "TrueNAS rejected the API key for %s. Check "
                        "vault_truenas_api_key, and that the key's account still "
                        "exists and is not revoked." % p["api_url"]
                    )
                )

            existing = client.call(
                "reporting.exporters.query", [["name", "=", p["name"]]]
            )

            if not existing:
                if module.check_mode:
                    module.exit_json(
                        changed=True,
                        created=True,
                        changes={"exporter": (None, p["name"])},
                        msg="Would create reporting exporter %s" % p["name"],
                    )
                created = client.call("reporting.exporters.create", desired)
                module.exit_json(
                    changed=True,
                    created=True,
                    exporter_id=created.get("id"),
                    msg="Created reporting exporter %s" % p["name"],
                )

            current = existing[0]
            changes = diff_exporter(current, desired)

            if not changes:
                module.exit_json(
                    changed=False,
                    created=False,
                    exporter_id=current.get("id"),
                    msg="Reporting exporter %s already converged" % p["name"],
                )

            if module.check_mode:
                module.exit_json(
                    changed=True,
                    created=False,
                    exporter_id=current.get("id"),
                    changes=changes,
                    msg="Would update reporting exporter %s" % p["name"],
                )

            client.call("reporting.exporters.update", current["id"], desired)
            module.exit_json(
                changed=True,
                created=False,
                exporter_id=current["id"],
                changes=changes,
                msg="Updated reporting exporter %s" % p["name"],
            )
    except Exception as exc:  # noqa: BLE001 - surface the API error verbatim
        module.fail_json(
            msg="TrueNAS API call failed against %s: %s: %s"
            % (p["api_url"], type(exc).__name__, exc)
        )


if __name__ == "__main__":
    main()
