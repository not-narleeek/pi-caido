# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2025-07-16

### Fixed
- Made `npm run check` (TypeScript type-check) actually pass. It had never
  been runnable because `@types/node` was missing, and it surfaced 6 latent
  type errors: every tool's `execute` returned `content: [{ type: "text" }]`
  where TypeScript widened `"text"` to `string`, breaking the
  `AgentToolResult` contract. Added `as const` to all 9 content literals, and
  added the missing `details` field to the `caido_scope` list-action return.
  Harmless at runtime, but the type-check is now functional and enforceable
  in CI.

### Changed
- Added `@types/node` and `typescript` as dev dependencies (were missing —
  the `check` script needs them).

## [0.1.0] - 2025-07-16

### Added
- Initial release as a standalone, distributable pi package.
- `extensions/caido.ts` — pi extension exposing six tools
  (`caido_request`, `caido_search`, `caido_get_request`, `caido_history`,
  `caido_proxy`, `caido_scope`), a `/caido` command, and a live status footer.
- `scripts/caido.py` — dependency-free (stdlib-only) GraphQL automation client
  and standalone CLI for Caido (instance discovery/headless launch, guest or
  API-token auth, project bootstrap, Repeater send, HTTPQL search, scope,
  export, proxy + CA cert).
- Self-contained Python script resolution: `$CAIDO_PY` → bundled
  `scripts/caido.py` → legacy `~/.pi/scripts/caido.py`.
- Config via `CAIDO_API_TOKEN`, `CAIDO_PY`, `CAIDO_STATE_DIR`.
- `package.json` pi manifest (`pi.extensions`), `tsconfig.json` for local
  type-checking, MIT license, README, and architecture/HTTPQL docs.

[Unreleased]: https://github.com/not-narleeek/pi-caido/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/not-narleeek/pi-caido/releases/tag/v0.1.0
