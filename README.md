# dpdpkit-core

The rules engine behind [dpdpkit](https://github.com/dpdpkit-dev). It provides a consent ledger, versioned
notices, rights requests and grievances, retention and erasure, audit evidence, and a CLI for India's
Digital Personal Data Protection Act, 2023 and DPDP Rules, 2025.

> **Disclaimer.** dpdpkit is software that helps you implement obligations under India's Digital Personal
> Data Protection Act, 2023 and the DPDP Rules, 2025. It does not provide legal advice and does not
> guarantee compliance. Decisions about notices, purposes, retention and incident reporting must be made
> by you or your counsel.

Core has no web framework and no database driver. Framework adapters
([`dpdpkit-fastapi`](https://github.com/dpdpkit-dev/dpdpkit-fastapi), `dpdpkit-django`) implement its
`Repository` protocol and expose it over the REST contract defined in
[`dpdpkit-spec`](https://github.com/dpdpkit-dev/dpdpkit-spec).

## Install

```bash
pip install dpdpkit            # core + policy packs + CLI
pip install "dpdpkit[fastapi]" # with the FastAPI adapter
```

## Use

```python
from dpdpkit import InMemoryRepository, MemoryNotifier, kit_from_config, load_config, sync_notice

config = load_config("dpdpkit.yaml")  # `dpdpkit init` writes a starter file
kit = kit_from_config(config, InMemoryRepository(), notifier=MemoryNotifier(), signing_key="change-me")
sync_notice(kit, config)  # publishes the notice if its text changed

kit.consent.grant("u_42", "marketing")  # recorded against the current notice version
kit.consent.check("u_42", "marketing")  # True
kit.consent.withdraw("u_42", "marketing")  # one call, same as granting
kit.rights.open("u_42", "erasure")  # due dates come from the policy pack
kit.retention.run()  # warns, then erases only after a delivered warning
kit.ledger.verify()  # raises LedgerTampered(row_id) if a row was edited
```

## Modules

| Module | Responsibility |
| --- | --- |
| `dpdpkit.policy` | Load and validate a policy pack; typed accessors for every legal number |
| `dpdpkit.registry` | Purposes, data items, legal basis (consent / DPDP Act s.7), processors, data stores |
| `dpdpkit.notices` | Versioned notices, content hash, locale fallback, required-link check |
| `dpdpkit.ledger` | Append-only SHA-256 hash chains (consent + audit) per tenant, `verify()`, root-hash export |
| `dpdpkit.consent` | `grant`, `deny`, `withdraw`, `check`, current state, signed receipts |
| `dpdpkit.rights` | Access, correction, completion, updating, erasure, nomination and grievance requests |
| `dpdpkit.retention` | Erasure schedules, pre-erasure warnings, legal holds, `needs_attention`, processor fan-out |
| `dpdpkit.events` | In-process event bus and HMAC-signed webhooks |
| `dpdpkit.notify` | Notifier protocol with delivery receipts; memory, console and SMTP transports |
| `dpdpkit.ses` | Amazon SES email transport (`dpdpkit-core[ses]`); optional tracked delivery from SES events via SQS |
| `dpdpkit.export` | Principal export (JSON, HTML) and evidence packs (CSV + PDF zip) |
| `dpdpkit.telemetry` | Opt-in anonymous usage telemetry; off by default (see below) |

## Fail-safe rules

- An erasure runs only after its warning is recorded as **delivered**, and only once the full warning
  period has passed since delivery. Otherwise it moves to a `needs_attention` queue.
- Missing contact details, failed deliveries, failing erasure handlers and legal holds all stop erasure.
- Every state change writes a ledger or audit entry before it takes effect.
- A missing or invalid policy pack means the kit refuses to start. There are no built-in defaults.
- There is no function that submits anything to an authority.

## CLI

```bash
dpdpkit init                                   # starter dpdpkit.yaml
dpdpkit policy show | list | diff OLD NEW
dpdpkit ledger verify --kit myapp.dpdp:kit     # exit code 2 if tampered
dpdpkit retention preview --kit myapp.dpdp:kit
dpdpkit export --principal u_42 --format html --kit myapp.dpdp:kit
dpdpkit export --evidence --out evidence.zip --kit myapp.dpdp:kit
dpdpkit telemetry status | enable | disable | show
```

## Telemetry

dpdpkit does not call home. Telemetry is **off by default** and stays off unless you opt in.

| To | Do |
| --- | --- |
| See whether it is on, and why | `dpdpkit telemetry status` |
| Opt in for your user | `dpdpkit telemetry enable` (stores a random install id in `telemetry.json`) |
| Opt out and delete the install id | `dpdpkit telemetry disable` |
| Opt in for one process or container | `DPDPKIT_TELEMETRY=1` (a fresh random id per process unless you ran `enable`) |
| Force it off, whatever is stored | `DPDPKIT_TELEMETRY=0` or `DO_NOT_TRACK=1` |
| Choose the collector | `DPDPKIT_TELEMETRY_URL=https://…` (HTTPS only) |
| See exactly what would be sent | `dpdpkit telemetry show` |

`telemetry.json` lives in `DPDPKIT_CONFIG_DIR` if set, else `%APPDATA%\dpdpkit` on Windows, else
`$XDG_CONFIG_HOME/dpdpkit` or `~/.config/dpdpkit`.

There is no default collector yet, so opting in sends nothing until `DPDPKIT_TELEMETRY_URL` is set.

When it is on and a collector is set, dpdpkit posts one small JSON document per event per process. It sends
`kit_started` when a `Kit` is created and `cli` when a `dpdpkit` command runs. Each document is sent from a
background thread with a 2-second timeout, and failures are ignored. A document holds only these fields:

| Field | Example |
| --- | --- |
| `schema` | `1` |
| `install_id` | random hex id, not derived from your machine |
| `event` | `kit_started` or `cli` |
| `dpdpkit_core`, `packages` | installed `dpdpkit*` distributions and versions |
| `python`, `os` | `3.12`, `Linux` |
| `policy` (`kit_started`) | a shipped pack id such as `dpdp-rules-2025.v1`, or `custom` |
| `command` (`cli`) | the top-level command name, such as `ledger` |

It never includes principals, tenants, consent or request data, configuration, notice text, file paths or
host names. The code refuses any other field.
Any HTTP request reveals the sender's IP address to the collector. A collector for dpdpkit must not store it.

## Writing an adapter

Implement `dpdpkit.repository.Repository` (see its docstring for the rules) and run the
`dpdpkit-conformance` suite from `dpdpkit-spec` against your HTTP layer.

## Development

```bash
uv venv && uv pip install -e ../dpdpkit-policies -e ".[dev]"
pytest && ruff check . && mypy
```

## Licence

Apache-2.0.
