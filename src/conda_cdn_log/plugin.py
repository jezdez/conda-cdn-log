"""Conda registration, without transport changes during discovery."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from conda.plugins import hookimpl

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace
    from collections.abc import Iterator

    from conda.plugins.types import CondaPreCommand, CondaSubcommand


def start(command: str) -> None:
    if directory := os.environ.get("CONDA_CDN_LOG_DIR"):
        from pathlib import Path

        from .capture import install

        hosts = os.environ.get(
            "CONDA_CDN_LOG_HOSTS", "repo.anaconda.com,conda.anaconda.org"
        )
        install(Path(directory), frozenset(hosts.lower().split(",")))


@hookimpl
def conda_pre_commands() -> Iterator[CondaPreCommand]:
    from conda.plugins.types import CondaPreCommand

    yield CondaPreCommand(
        name="cdn-log",
        action=start,
        run_for={
            "create",
            "install",
            "update",
            "upgrade",
            "remove",
            "uninstall",
            "search",
            "list",
            "info",
            "notices",
            "env_create",
            "env_update",
        },
    )


def configure_parser(parser: ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="+", help="JSONL files or artifact directories")
    parser.add_argument(
        "--github-summary",
        action="store_true",
        help="also append to GITHUB_STEP_SUMMARY",
    )


def summarize(args: Namespace) -> int:
    from .summary import execute

    return execute(args)


@hookimpl
def conda_subcommands() -> Iterator[CondaSubcommand]:
    from conda.plugins.types import CondaSubcommand

    yield CondaSubcommand(
        name="cdn-log",
        summary="Summarize captured HTTP diagnostics for CI.",
        action=summarize,
        configure_parser=configure_parser,
    )
