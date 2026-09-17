# Registro 📟

**A terminal-first database workspace for modern engineers**

**Registro** is a TUI database client built on a clean async core and a plugin-first architecture. It combines a rich query editor, lazy schema browser, virtualized result tables, and a fuzzy command palette — all inside your terminal.

---

## ✨ Features

- **🎹 Keyboard-First Layout**: Fully navigatable using Vim keys or custom layout keymaps.
- **⚡ Asynchronous Execution Engine**: Run queries in the background. Long-running queries will never freeze the UI, and can be cancelled with a single keystroke (`Ctrl+B`).
- **🔌 Pluggable Architecture**: Extend functionality with custom panels, commands, and hooks subscribing to runtime event lifecycles.
- **📊 Interactive TUI**: Rich results viewer, schema tree explorer, syntax highlighting, and batch rendering.
- **🛠️ Command Line Interface**: Fast, standalone utilities to connect, test connections, and execute queries directly to standard output in table, CSV, or JSON formats.

---

## 🗄️ Supported Databases

Registro is database-agnostic and loads drivers dynamically via optional extras.

| Database | Extra | Adapter Protocol | Details |
| :--- | :--- | :--- | :--- |
| **DuckDB** | `[duckdb]` | `duckdb` | Direct in-process analytics engine |
| **SQLite** | `[sqlite]` | `aiosqlite` | Async local database access |
| **PostgreSQL** | `[postgres]` | `asyncpg` | Native async driver for Postgres |
| **MySQL** | `[mysql]` | `asyncmy2` | Native async driver for MySQL |
| **All Drivers** | `[all]` | — | Installs all supported drivers |

---

## 🚀 Installation

Registro requires **Python 3.13 or newer**. It is recommended to install using `pipx` or `uv` to keep dependencies isolated.

### Using `uv` (Recommended)

```bash
# Install with all database drivers
uv tool install "registro[all]"

# Install with PostgreSQL and SQLite support only
uv tool install "registro[postgres,sqlite]"
```

### Using `pipx`

```bash
# Install with DuckDB support
pipx install "registro[duckdb]"
```

---

## 📖 Quick Start

You can run queries one-shot from the shell or jump into the interactive terminal user interface.

### Running a One-Shot Query

Use the `run` command to execute a single query. By default, results render as a styled Rich table:

```bash
registro run --dsn "sqlite:///./my_database.db" "SELECT * FROM users LIMIT 5"
```

#### Output Formats (`--format` / `-f`)

Format output as `table` (default), `csv`, or `json`. Machine-readable formats (`csv`, `json`) write clean unformatted data to `stdout` so results can be piped directly into `jq`, files, or CLI pipelines, while row counts and notices go to `stderr`:

```bash
# Export query results as clean CSV
registro run --dsn "duckdb:///:memory:" --format csv "SELECT 1 AS id, 'alice' AS name" > users.csv

# Pipe JSON output directly into jq
registro run --dsn "postgres://user:pass@localhost:5432/my_db" -f json "SELECT id, name FROM users" | jq '.[0]'
```

#### Additional Execution Options

- `--limit <N>`: Maximum rows to fetch and print (default: `200`).
- `--timeout <seconds>`: Query execution timeout in seconds. Set to `0` to disable timeout. Defaults to `engine.default_timeout_seconds` from configuration (60s).
- `--config <path>`: Load configuration overrides from a specific TOML file.

### Starting the Interactive TUI

Launch the full interactive terminal user interface:

```bash
# Connect directly to a database
registro tui --dsn "postgres://user:pass@localhost:5432/my_db"

# Or launch without a DSN to open the interactive connection dialog
registro tui
```

### Command Reference

```bash
# Display help and options
registro --help

# Print version and discovered database adapters
registro version

# Sanity check database connection
registro connect --dsn "mysql://root:secret@127.0.0.1/test"

# Use a specific config file across commands
registro run --config ./registro.toml --dsn "duckdb:///:memory:" "SELECT 42"

# List discovered plugins
registro plugins list
```

---

## 🎹 Keybindings

Registro provides a responsive, keyboard-driven interface with collision-free shortcuts.

### Global Shortcuts

Available everywhere across the application:

| Key | Action | Description |
| :--- | :--- | :--- |
| `Ctrl+Enter` / `Ctrl+J` | Run Query | Execute SQL query in the active editor tab |
| `Ctrl+B` | Cancel Query | Abort the running query without freezing the UI |
| `Ctrl+P` | Command Palette | Open fuzzy command palette |
| `Ctrl+T` | New Tab | Open a new SQL query editor tab |
| `Ctrl+O` | Close Tab | Close the active query tab |
| `Ctrl+R` | Refresh Schema | Refresh database catalog and schema tree |
| `Ctrl+G` | Query History | Browse and re-run past queries |
| `?` | Help | Show keyboard shortcuts modal |
| `Ctrl+Q` | Quit | Exit Registro |

> [!NOTE]
> Query cancellation is bound to `Ctrl+B` (break) and tab closing to `Ctrl+O` to avoid colliding with text editor controls (`Ctrl+C` for copying text and `Ctrl+W` for deleting words).

### Results Grid Shortcuts

Active when the query results table is focused:

| Key | Action | Description |
| :--- | :--- | :--- |
| `Ctrl+Y` | Copy Cell | Copy selected cell value to clipboard |
| `Ctrl+E` | Export CSV | Export current query result set to CSV file |

### Vim Keymap Mode

Set `keymap = "vim"` in `config.toml` (or `REGISTRO_UI__KEYMAP="vim"`) to enable Vim-style navigation:

| Key | Action | Description |
| :--- | :--- | :--- |
| `:` | Command Palette | Open fuzzy command palette (Ex command) |
| `gg` | Editor Top | Move cursor to the top of the editor |
| `G` (`Shift+G`) | Editor Bottom | Move cursor to the bottom of the editor |

---

## ⚙️ Configuration

Registro configuration is loaded in layers: **Defaults ➡️ TOML Configuration ➡️ Environment Variables ➡️ CLI Flags**.

This applies to `registro tui`, `registro run`, and `registro connect` alike — each accepts `--config` to point to a specific file.

The default configuration file is searched at `~/.config/registro/config.toml`. The configuration file must have secure permissions (e.g., `0600`) if it contains credentials.

Here is an example config file (see [config.example.toml](config.example.toml)):

```toml
[ui]
theme = "monokai"               # Theme: "monokai" (warm) | "nord" (cool)
keymap = "default"              # Keymap: "default" | "vim"
row_batch_size = 1000           # Query result batch loading size (10 to 100,000)
max_display_rows = 10000        # Caps rows stored in results view (0 for unlimited)

[engine]
default_timeout_seconds = 60.0  # Soft limit for query runs (seconds)
max_history_entries = 1000      # Max CLI query history entries (min 10)
pool_size = 5                   # Connection pool size (1 to 64)
history_path = "~/.local/share/registro/history.db"  # Query history database path

[logging]
level = "INFO"                  # Log level: DEBUG | INFO | WARNING | ERROR
json_lines = false              # Emit JSON logs (auto-selects JSON in non-TTY/Docker)

[plugins]
enabled = []                    # Allowlist (empty loads all discovered)
disabled = []                   # Blocklist
```

### Environment Overrides

Any configuration value can be overridden via environment variables using the pattern `REGISTRO_<SECTION>__<FIELD>`.

Examples:
```bash
# Override the UI theme
export REGISTRO_UI__THEME="nord"

# Override keymap to vim
export REGISTRO_UI__KEYMAP="vim"

# Cap every query at 10 seconds, across TUI and CLI
export REGISTRO_ENGINE__DEFAULT_TIMEOUT_SECONDS=10

# Set default connection DSN
export REGISTRO_DSN="postgres://user:pass@localhost:5432/my_db"
```

---

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📜 Changelog

Detailed release history and version changes can be found in [CHANGELOG.md](CHANGELOG.md).

## 🔐 Security

If you discover a security vulnerability, please see our [Security Policy](SECURITY.md).

## 📝 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

---

**Author**: [Salvatore Corvaglia](https://github.com/salvatorecorvaglia)