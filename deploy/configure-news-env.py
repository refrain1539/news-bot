#!/usr/bin/env python3
"""Interactively set the non-Discord values in the private news bot env file."""

from getpass import getpass
from pathlib import Path
import os
import stat
import sys


TARGET = Path("/home/seiya/.config/research-bots/news.env")
KEYS = ("GEMINI_API_KEY", "DISCORD_CHANNEL_ID")


def prompt(key: str) -> str:
    while True:
        value = getpass(f"{key} (input is hidden): ").strip()
        if value:
            return value
        print("This value cannot be empty. Please try again.", file=sys.stderr)


def main() -> int:
    if not TARGET.is_file():
        print(f"Missing configuration file: {TARGET}", file=sys.stderr)
        return 1

    values = {key: prompt(key) for key in KEYS}
    original = TARGET.read_text(encoding="utf-8").splitlines()
    replaced = set()
    result = []
    for line in original:
        key, separator, _ = line.partition("=")
        if separator and key in values:
            result.append(f"{key}={values[key]}")
            replaced.add(key)
        else:
            result.append(line)
    for key in KEYS:
        if key not in replaced:
            result.append(f"{key}={values[key]}")

    temp = TARGET.with_name(f".{TARGET.name}.tmp")
    temp.write_text("\n".join(result) + "\n", encoding="utf-8")
    os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp, TARGET)
    print("Saved Gemini API key and Discord channel ID to news.env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
