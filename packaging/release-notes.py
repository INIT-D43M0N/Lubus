#!/usr/bin/env python3
"""Print the release notes of a version, taken from the changelog.

Usage:
    ./packaging/release-notes.py v6.1.2 [CHANGELOG.md]

The section of the changelog that belongs to the version is printed as it
is, so the notes of a release and the changelog never drift apart.
"""
import re
import sys


def extract(version: str, changelog: str) -> str:
    """Return the section of the changelog that belongs to the version."""
    with open(changelog, encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        text,
        re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version = argv[0].lstrip("v")
    changelog = argv[1] if len(argv) > 1 else "CHANGELOG.md"

    print(f"## Yaku {version}")
    print()
    notes = extract(version, changelog)
    if notes:
        print(notes)
    else:
        print(f"See the [changelog]({changelog}) for the changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
