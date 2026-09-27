# Changelog

All code packages in the dpdpkit release train share one version number.

## Unreleased

## 0.1.0a1 - 2026-09-27

### Added
- `Kit` facade; policy, registry, notices, ledger, consent, rights, retention, events, notify, export modules.
- Hash-chained consent ledger and audit trail with `verify()`, `verify_report()`, `root_hash()` and `check_root()`.
- `InMemoryRepository` and the `Repository` protocol for adapters.
- CLI: `init`, `policy show|list|diff`, `ledger verify|root`, `retention preview|run`, `export`.
- `dpdpkit` meta-package with the `fastapi` extra.
