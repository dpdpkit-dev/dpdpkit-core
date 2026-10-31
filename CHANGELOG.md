# Changelog

All code packages in the dpdpkit release train share one version number.

## Unreleased

### Added
- `Ledger.export_root(*sinks)` writes the root hash to each `RootSink` and publishes `ledger.root_exported`.
- `RootSink` protocol and `FileRootSink` (JSON lines; `read()`, `latest(tenant_id)`).
- CLI: `dpdpkit ledger root --out FILE` appends to an anchor file; `dpdpkit ledger check-root --anchors FILE`
  checks the chains against the newest anchor (exit 2 on mismatch).
- Event type constants `LEDGER_ROOT_EXPORTED` and `LEDGER_TAMPERED`.
- `dpdpkit.ses.SesNotifier` (optional `ses` extra, boto3). `track_delivery=True` keeps messages queued until
  SES reports delivery; `poll_ses_events()` applies Delivery/Bounce/Reject events from an SQS queue.
- `dpdpkit[ses]` extra on the meta-package.
- Deprecation support: `DpdpkitDeprecationWarning`, `warn_deprecated()`, `moved()`; a test enforces the
  standard wording and a two-minor-release minimum for every `@deprecated` in the source.

### Changed
- `Ledger.check_root()` refuses a root hash from another tenant (`ValidationFailed`).
- CI enforces at least 95% line and branch coverage on `ledger`, `retention` and `rights`.

## 0.1.0a1 - 2026-09-27

### Added
- `Kit` facade; policy, registry, notices, ledger, consent, rights, retention, events, notify, export modules.
- Hash-chained consent ledger and audit trail with `verify()`, `verify_report()`, `root_hash()` and `check_root()`.
- `InMemoryRepository` and the `Repository` protocol for adapters.
- CLI: `init`, `policy show|list|diff`, `ledger verify|root`, `retention preview|run`, `export`.
- `dpdpkit` meta-package with the `fastapi` extra.
