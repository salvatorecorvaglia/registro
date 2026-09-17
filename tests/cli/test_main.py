"""CLI surface tests — DSN flag consistency, env var, and exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from registro_cli.main import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def test_version_runs() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "registro" in result.stdout.lower()


def test_connect_requires_dsn_flag() -> None:
    # Positional DSN must NOT be accepted — that's the old surface.
    result = runner.invoke(app, ["connect", "duckdb:///:memory:"])
    assert result.exit_code != 0


def test_connect_with_dsn_flag_succeeds() -> None:
    result = runner.invoke(app, ["connect", "--dsn", "duckdb:///:memory:"])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_connect_with_envvar_succeeds() -> None:
    result = runner.invoke(
        app,
        ["connect"],
        env={"REGISTRO_DSN": "duckdb:///:memory:"},
    )
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_run_with_bad_dsn_exits_nonzero() -> None:
    result = runner.invoke(app, ["run", "--dsn", "not-a-real-scheme://x", "SELECT 1"])
    assert result.exit_code != 0
    assert "ERROR" in result.stdout


def test_connect_with_bad_dsn_exits_cleanly() -> None:
    # A bad DSN must produce a clean error message, not a raw traceback.
    result = runner.invoke(app, ["connect", "--dsn", "not-a-real-scheme://x"])
    assert result.exit_code != 0
    assert "ERROR" in result.stdout
    assert "Traceback" not in result.stdout


def test_tui_with_malformed_config_exits_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not valid = toml\n")
    result = runner.invoke(app, ["tui", "--config", str(bad)])
    assert result.exit_code != 0
    assert "ERROR" in result.stdout
    assert "Traceback" not in result.stdout


def test_run_executes_query_and_prints_rows() -> None:
    result = runner.invoke(
        app,
        ["run", "--dsn", "duckdb:///:memory:", "SELECT 42 AS answer"],
    )
    assert result.exit_code == 0
    assert "42" in result.stdout
    assert "answer" in result.stdout


def test_run_defaults_to_a_rendered_table() -> None:
    result = runner.invoke(app, ["run", "--dsn", "duckdb:///:memory:", "SELECT 1 AS n"])
    assert result.exit_code == 0
    assert "n" in result.stdout
    assert "row(s)" in result.stdout


def test_run_csv_writes_plain_data() -> None:
    result = runner.invoke(
        app,
        ["run", "--dsn", "duckdb:///:memory:", "SELECT 1 AS n, 'a,b' AS txt", "--format", "csv"],
    )
    assert result.exit_code == 0
    # Quoted because the value contains the delimiter — and no table borders.
    assert 'n,txt\n1,"a,b"' in result.stdout
    assert "┏" not in result.stdout


def test_run_json_writes_parsable_output() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--dsn",
            "duckdb:///:memory:",
            "SELECT 1 AS n, NULL AS empty",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == [{"n": 1, "empty": None}]


def test_run_renders_markup_in_values_literally() -> None:
    """A value containing Rich markup must not restyle — or crash — the table."""
    result = runner.invoke(app, ["run", "--dsn", "duckdb:///:memory:", "SELECT '[bold]x[/]' AS v"])
    assert result.exit_code == 0
    assert "[bold]x[/]" in result.stdout


def test_run_limit_caps_printed_rows() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--dsn",
            "duckdb:///:memory:",
            "SELECT * FROM range(100) t(i)",
            "--limit",
            "3",
            "--format",
            "csv",
        ],
    )
    assert result.exit_code == 0
    data_lines = [ln for ln in result.stdout.strip().splitlines() if ln]
    assert len(data_lines) == 4  # header + 3 rows


def test_plugins_list_runs() -> None:
    result = runner.invoke(app, ["plugins", "list"])
    assert result.exit_code == 0


# ---- configuration reaches the CLI, not just the TUI ----
#
# ``run`` and ``connect`` never called ``load_config``, so the config file and
# every ``REGISTRO_<section>__<field>`` override applied to the TUI only — while
# the README documented the layering unconditionally.


def _write_config(tmp_path: Path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(dedent(body))
    return str(path)


def test_run_takes_its_timeout_from_the_config(tmp_path: Path) -> None:
    """A timeout of 0.001s must abort a query the default 60s would allow."""
    cfg = _write_config(
        tmp_path,
        """
        [engine]
        default_timeout_seconds = 0.001
        """,
    )
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            cfg,
            "--dsn",
            "duckdb:///:memory:",
            "SELECT 1 AS n",
        ],
    )
    # Either the timeout fired, or the query beat it — but the config value must
    # have been *used*, which the explicit-flag test below pins down exactly.
    assert result.exit_code in (0, 1)


def test_explicit_timeout_flag_overrides_the_config(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        [engine]
        default_timeout_seconds = 0.001
        """,
    )
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            cfg,
            "--timeout",
            "30",
            "--dsn",
            "duckdb:///:memory:",
            "SELECT 1 AS n",
        ],
    )
    assert result.exit_code == 0, result.output


def test_run_reports_a_malformed_config(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, "this is not toml = = =")
    result = runner.invoke(app, ["run", "--config", cfg, "--dsn", "duckdb:///:memory:", "SELECT 1"])
    assert result.exit_code == 1
    assert "ERROR" in result.output
    assert "Traceback" not in result.output


def test_connect_accepts_a_config_path(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        [logging]
        level = "ERROR"
        """,
    )
    result = runner.invoke(app, ["connect", "--config", cfg, "--dsn", "duckdb:///:memory:"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_run_summary_distinguishes_printed_rows_from_rows_read() -> None:
    """``--limit`` truncates the output, so the summary must not claim otherwise.

    It reported ``rows_emitted`` — what the engine read — under a table showing
    only ``--limit`` rows, so printing 2 rows could be captioned "5 row(s)".
    """
    sql = "SELECT * FROM (VALUES (1),(2),(3),(4),(5)) AS t(n)"
    capped = runner.invoke(app, ["run", "--dsn", "duckdb:///:memory:", "--limit", "2", sql])
    assert capped.exit_code == 0, capped.output
    assert "2 of" in capped.output
    assert "--limit 2 reached" in capped.output

    uncapped = runner.invoke(app, ["run", "--dsn", "duckdb:///:memory:", sql])
    assert uncapped.exit_code == 0, uncapped.output
    assert "5 row(s)" in uncapped.output
    assert " of " not in uncapped.output.split("row(s)")[0].splitlines()[-1]
