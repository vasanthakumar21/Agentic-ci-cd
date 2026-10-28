"""Smoke tests for the scaffold.

Confirm the package imports and the CLI entry point runs. Real behaviour tests land
alongside the IR schema, plugins, and generator core in later tasks.
"""

from __future__ import annotations

from pathlib import Path

import cicdgen
from cicdgen.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_package_version_exposed() -> None:
    assert cicdgen.__version__ == "0.1.0"


def test_cli_runs_with_no_args() -> None:
    assert main([]) == 0


def test_expected_folder_layout_exists() -> None:
    """The design §2 folder layout must be present."""
    for folder in (
        "ir",
        "plugins/ci",
        "plugins/target",
        "templates",
        "generator",
        "bootstrap",
        "docs",
        "examples",
    ):
        assert (REPO_ROOT / folder).is_dir(), f"missing folder: {folder}"


def test_gitignore_excludes_local_cicd_config() -> None:
    """R6/R9: .cicd/*.local.* must be git-ignored."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".cicd/*.local.*" in gitignore
