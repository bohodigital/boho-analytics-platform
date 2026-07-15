"""Small, dependency-free command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__
from .config import ConfigError, load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boho-analytics",
        description="Validate and operate Boho Analytics Platform installations.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    config = commands.add_parser("config", help="configuration operations")
    config_commands = config.add_subparsers(dest="config_command")
    validate = config_commands.add_parser("validate", help="validate a non-secret TOML config")
    validate.add_argument("path")
    return parser


def _validate_config(path: str) -> int:
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": config.schema_version,
                "clients": len(config.clients),
                "sites": len(config.sites),
                "connections": len(config.connections),
                "bindings": len(config.bindings),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "config" and args.config_command == "validate":
        return _validate_config(args.path)
    parser.print_help(sys.stderr)
    return 2
