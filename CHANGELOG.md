# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
