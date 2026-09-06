# Changelog

All notable changes to ATLAS V2. Format follows Keep a Changelog; this project
versions by implementation phase rather than semver until Phase 11.

## [Unreleased]

### Phase 1 — Safety primitives
- Typed configuration with no defaults for any financial parameter (ADR-006)
- Domain models with `Decimal` money types; `float` rejected at construction (ADR-003)
- Fail-safe kill switch: dual-store, disagreement and unreadability resolve to ARMED
  (KILL-01..06, ADR-004)
- Hash-chained append-only audit log with tamper detection (AI-07, ADR-005)
- SQLite/WAL persistence with STRICT tables, foreign keys and CHECK constraints
  (ADR-001, ADR-002)

### Phase 0 — Repository and toolchain
- Repository scaffold, packaging, ruff + mypy strict + pytest, GitHub Actions CI
- Governing specification committed (`docs/SPECIFICATION.md`)
- Architecture, security, configuration, traceability, runbook and ADR documentation
