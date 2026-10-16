"""Fail unless a release tag matches the version in pyproject.toml.

    python ci/check_tag_version.py v0.3.0

Compared as PEP 440 versions, so `v0.3.0-rc.1` matches `version = "0.3.0rc1"`.
Runs in the toolchain image (ci/Dockerfile), which has `packaging`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version


def main(tag: str) -> int:
    if not tag.startswith("v"):
        print(f"tag {tag!r} doesn't start with 'v'", file=sys.stderr)
        return 1
    declared = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    try:
        tagged = Version(tag[1:])
    except InvalidVersion:
        print(f"tag {tag!r} is not a valid version", file=sys.stderr)
        return 1
    if tagged != Version(declared):
        print(
            f"tag {tag!r} doesn't match pyproject.toml version {declared!r} - "
            "bump the version (and uv.lock) before tagging",
            file=sys.stderr,
        )
        return 1
    print(f"tag {tag} matches pyproject.toml version {declared}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check_tag_version.py <tag>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
