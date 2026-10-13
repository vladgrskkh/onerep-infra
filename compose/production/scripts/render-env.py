#!/usr/bin/env python3
"""Render selected environment values to a private Compose .env file."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def compose_quote(value: str) -> str:
    """Use Compose dotenv double quotes, including its newline escape syntax."""
    return json.dumps(value, ensure_ascii=False)


def render(output: Path, names: list[str], environment: dict[str, str]) -> None:
    if not names:
        raise ValueError("at least one variable name is required")
    if len(set(names)) != len(names):
        raise ValueError("duplicate variable name")
    for name in names:
        if not ENV_NAME.fullmatch(name):
            raise ValueError("invalid variable name")
        if name not in environment:
            raise ValueError(f"required variable is missing: {name}")
        if not environment[name]:
            raise ValueError(f"required variable is empty: {name}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{output.name}.",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            for name in names:
                temporary.write(f"{name}={compose_quote(environment[name])}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
        output.chmod(0o600)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--var", action="append", dest="names", required=True)
    arguments = parser.parse_args()
    try:
        render(arguments.output, arguments.names, dict(os.environ))
    except (OSError, ValueError) as error:
        message = str(error)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
