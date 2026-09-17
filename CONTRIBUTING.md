# Contributing to Registro 📟

Thank you for your interest in contributing to **Registro**! We welcome contributions, bug reports, feature requests, and security improvements from the community.

---

## 🛠️ Development Setup

Registro uses [uv](https://github.com/astral-sh/uv) for fast, modern Python package management and workflow orchestration.

### Prerequisites

- **Python 3.13 or newer**
- **uv** (Install via `curl -LsSf https://astral.sh/uv/install.sh | sh` or your package manager)

### Quick Start

1. **Fork and Clone the Repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/registro.git
   cd registro
   ```

2. **Synchronize Dependencies**
   Initialize the virtual environment and install all packages (including development packages and all database extras):
   ```bash
   uv sync --all-extras --dev
   ```
   *Alternatively, run `./scripts/dev.sh` to initialize the environment.*

3. **Activate the Environment**
   ```bash
   source .venv/bin/activate
   ```

---

## ⚙️ Coding Standards & Tools

We use strict quality gates to maintain codebase health. All of these run automatically on CI, but you should run them locally before submitting changes.

### Developer Scripts

We provide dedicated helper scripts under `scripts/` to streamline local workflows:

| Script | Purpose |
| :--- | :--- |
| `./scripts/dev.sh` | Synchronizes virtual environment with all extras and dev dependencies |
| `./scripts/fmt.sh` | Formats code with Ruff and applies safe linter autofixes |
| `./scripts/lint.sh` | Runs Ruff format check, Ruff lint check, and MyPy strict type check |
| `./scripts/test.sh` | Runs the offline test suite with coverage reporting |
| `./scripts/check.sh` | The pre-PR quality gate (runs `./scripts/lint.sh` and `./scripts/test.sh`) |

### Manual Linting & Formatting

We use **Ruff** for formatting and linting, and **MyPy** for strict static type checking:

- **Check formatting:**
  ```bash
  uv run ruff format --check .
  ```
- **Format code:**
  ```bash
  uv run ruff format .
  # Or run ./scripts/fmt.sh
  ```
- **Lint code:**
  ```bash
  uv run ruff check .
  ```
- **Strict type checking:**
  ```bash
  uv run mypy --config-file=mypy.ini src
  ```

---

## 🧪 Testing

We use **Pytest** for running our test suite with strict coverage requirements.

- **Run unit tests (offline suite):**
  ```bash
  uv run pytest -m "not integration" --cov=src
  # Or simply:
  ./scripts/test.sh
  ```
- **Run integration tests (requires Docker daemon for PostgreSQL and MySQL):**
  ```bash
  ./tests/scripts/test-integration.sh
  ```

### Coverage Quality Gate

Our test suite enforces an **84% minimum test coverage gate** (`fail_under = 84` in `pyproject.toml`). The offline suite run must satisfy this gate on its own without relying on server-backed integration tests.

### The Ultimate Quality Gate

Before submitting any Pull Request, ensure that formatting, linting, type-checking, and test coverage all pass cleanly by running:
```bash
./scripts/check.sh
```

---

## 🔌 Writing Plugins

Registro features an extensible, plugin-first architecture. You can contribute new plugins or create external plugins to add dockable panels, custom commands, and event listeners.

### Plugin Protocol

A Registro plugin implements the structural `Plugin` protocol (`registro_core.protocols.plugin.Plugin`):

```python
from registro_core.protocols.plugin import PluginManifest
from registro_plugins.context import PluginContext


class MyPlugin:
    manifest = PluginManifest(
        id="my_plugin",
        name="My Plugin",
        version="1.0.0",
        description="A custom Registro plugin",
    )

    def on_load(self, ctx: PluginContext) -> None:
        """Called when the plugin is loaded."""
        ...

    def on_unload(self, ctx: PluginContext) -> None:
        """Called when the plugin is unloaded or during app shutdown."""
        ...
```

### Packaging & Discovery

Plugins are discovered dynamically via Python entry points in the `registro.plugins` group. Add the following to your `pyproject.toml`:

```toml
[project.entry-points."registro.plugins"]
my_plugin = "my_package.plugin:MyPlugin"
```

You can verify that Registro discovers your plugin by running:
```bash
registro plugins list
```

### Subscribing to Event Bus Hooks

Plugins can subscribe to asynchronous lifecycle events on the `EventBus`. Key events defined in [events.py](src/registro_core/events.py) include:

- `ConnectionOpened` / `ConnectionClosed`
- `QueryStarted` / `QueryProgress` / `QueryCompleted` / `QueryFailed` / `QueryCancelled`
- `SchemaRefreshed`
- `PluginLoaded` / `PluginUnloaded`

The `@hook` decorator tags a method with the target event name. In `on_load`, subscribe your handler to the event class via `ctx.subscribe`:

```python
from registro_core.events import QueryStarted
from registro_plugins.api import hook, hook_event, is_hook
from registro_plugins.context import PluginContext


class QueryLoggerPlugin:
    manifest = PluginManifest(id="query_logger", name="Query Logger")

    @hook("query.started")
    async def handle_query_started(self, ev: QueryStarted) -> None:
        # Asynchronously handle query start
        pass

    def on_load(self, ctx: PluginContext) -> None:
        for name in dir(self):
            fn = getattr(self, name)
            if is_hook(fn) and hook_event(fn) == "query.started":
                ctx.subscribe(QueryStarted, fn)

    def on_unload(self, ctx: PluginContext) -> None:
        pass
```

### Registering Commands and Panels

Plugins can contribute actions to the fuzzy command palette (`Ctrl+P`) and dockable panels to the main UI:

```python
from textual.widgets import Static
from registro_plugins.api import Command, Panel
from registro_plugins.context import PluginContext


class AnalyticsPlugin:
    manifest = PluginManifest(id="analytics", name="Analytics")

    def on_load(self, ctx: PluginContext) -> None:
        # Register a command for the command palette (Ctrl+P)
        async def run_diagnostics(context: PluginContext) -> None:
            context.logger.info("Diagnostics started")

        ctx.register_command(
            Command(
                id="analytics.diagnostics",
                title="Run Database Diagnostics",
                handler=run_diagnostics,
                category="Analytics",
            )
        )

        # Register a dockable panel ("left", "right", or "bottom")
        def create_panel(_context: PluginContext) -> Static:
            return Static("★ Analytics Rail Content", id="analytics-panel-content")

        ctx.register_panel(
            Panel(
                id="analytics.sidebar",
                title="Analytics",
                placement="right",
                factory=create_panel,
                initial_size=30,
            )
        )

    def on_unload(self, ctx: PluginContext) -> None:
        pass
```

### Context & Host Services

Each plugin receives an isolated `PluginContext` providing:
- `ctx.subscribe(event_class, handler)`: Register an async event handler.
- `ctx.publish(event)`: Publish custom events across the event bus.
- `ctx.register_command(command)`: Add an action to the fuzzy command palette.
- `ctx.register_panel(panel)`: Mount a sidebar or bottom panel into the TUI grid.
- `ctx.get_service(name)`: Access host services (such as `"introspector"`, `"history"`, or `"config"`).
- `ctx.logger`: Context-bound structured logger (`logger.bind(plugin_id=...)`).

---

## 📥 Submitting a Pull Request

1. Create a logical feature branch: `git checkout -b feature/my-cool-feature`.
2. Write tests covering your implementation.
3. Verify that `./scripts/check.sh` passes successfully (formatting, linting, type checks, and 84% coverage gate).
4. Commit your changes with clear, descriptive commit messages.
5. Push to your fork and open a Pull Request.

---

Happy coding! 📟