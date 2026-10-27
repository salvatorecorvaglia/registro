# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add compatible stable-release checks, background TUI notifications, and confirmed uv/pipx tool upgrades with a dry-run option.

### Changed

- Update the pinned Ruff development dependency to 0.17.1.
- Update the locked `certifi` development dependency to 2026.10.23, which drops the emSign ECC Root CA - G3 root after eMudhra revoked intermediates with compromised keys.
- Cancel in-progress CI runs when a newer run starts for the same workflow and ref, and run CI for changes under `docs/` unless all changed files are Markdown.
- Remove the pull-request dependency review job.
- Require an existing `v`-prefixed tag and the reusable CI checks before publishing a release.


### Fixed

- Avoid exponential regular-expression backtracking when detecting explicit transactions in SQL with repeated leading comment delimiters, using a linear-time comment scan.

## [1.1.0] - 2026-10-15

### Added

- Execute selected SQL text in the TUI editor, with guidance when a database rejects multiple statements.
- Add `Ctrl+L` to change the row display limit, with a warning when unlimited results are enabled.
- Show affected-row counts for statements without result sets in the CLI and TUI, and show query errors in history.
- Warn when explicit transactions are opened in the TUI, where each run uses a pooled connection and transactions do not persist between runs.

### Changed

- Rename the Python distribution to `registro-db` and update installation documentation.
- Include DuckDB as a core dependency, relax selected dependency pins, and move `cryptography` to the MySQL and `all` extras.
- Pin the Docker build's `uv` version and omit the application source tree from the runtime image.
- Use Ubuntu 24.04 for GitHub Actions runners.
- Expose the TUI's schema adapter as `AppState.introspection_adapter`, retaining `adapter` as a deprecated read-only alias for plugins.

### Fixed

- Treat CLI and TUI row limits as successful, truncated queries rather than cancellations.
- Preserve PostgreSQL writes with `RETURNING` when results are truncated, and report commit failures before marking fully consumed queries successful.
- Clean up PostgreSQL transactions through the driver's public API and replace pooled connections that fail reset, preserving connected preloaded adapters such as in-memory databases.
- Publish query failure and cancellation events when stream setup fails, and distinguish failed, cancelled, and successful results in the TUI.
- Keep schema introspection errors from closing the TUI and preserve their status messages after connecting.
- Apply MySQL DSN TLS settings through an SSL context, including CA verification and client certificates.
- Preserve IPv6 brackets when rendering DSNs and masking passwords.
- Skip plugins disabled by entry-point name before importing them, and match plugin filters against both entry-point names and manifest IDs.
- Resolve the installed version using the `registro-db` distribution name.
- Honor the configured row batch size in CLI queries and accept lowercase logging levels.
- Avoid changing permissions on existing custom history directories.
- Give CSV exports unique filenames and refuse to overwrite existing files.
- Require both keys for Vim's `gg` sequence, give query editors unique widget IDs, and retain and cancel all command-palette tasks during shutdown.

## [1.0.0] - 2026-09-23

### Added

- First implementation of Registro.
