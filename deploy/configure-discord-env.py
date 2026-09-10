#!/usr/bin/env python3
"""Interactively set the private Discord bot tokens without echoing them."""

from getpass import getpass
from pathlib import Path
import os
import stat
import sys


TARGETS = (
    ("news", Path("/home/seiya/.config/research-bots/news.env")),
    ("arxiv", Path("/home/seiya/.config/research-bots/arxiv.env")),
)


def prompt(label: str) -> str:
    while True:
        value = getpass(f"{label} Discord Bot Token (input is hidden): ").strip()
        if value:
            return value
        print("This value cannot be empty. Please try again.", file=sys.stderr)


def replace_token(path: Path, value: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    result = []
    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key == "DISCORD_BOT_TOKEN":
            result.append(f"DISCORD_BOT_TOKEN={value}")
            found = True
        else:
            result.append(line)
    if not found:
        result.append(f"DISCORD_BOT_TOKEN={value}")
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text("\n".join(result) + "\n", encoding="utf-8")
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, path)


def main() -> int:
    for label, path in TARGETS:
        replace_token(path, prompt(label))
    print("Saved Discord Bot Token values to news.env and arxiv.env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
