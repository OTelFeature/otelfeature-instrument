"""Fail unless a release tag is well-formed and matches the package version.

    python ci/check_tag_version.py v0.3.0

Accepted tags: `vX.Y.Z`, or `vX.Y.Z-rc.N` for a pre-release (spec C-006).
The tag must equal `version` in pyproject.toml as a PEP 440 version, so
`v0.3.0-rc.1` needs `version = "0.3.0rc1"` - and `__version__` in the package
must say the same. Prints `prerelease=true|false`, which the release
workflow appends to $GITHUB_OUTPUT.

Runs in the toolchain image (ci/Dockerfile), which has `packaging`.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.version import Version

TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-rc\.(0|[1-9]\d*))?")
DUNDER = re.compile(r'^__version__ = "([^"]+)"$', re.M)


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(tag: str) -> int:
    match = TAG.fullmatch(tag)
    if not match:
        return fail(f"tag {tag!r} is not vX.Y.Z or vX.Y.Z-rc.N")
    tagged = Version(tag[1:])
    declared = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if tagged != Version(declared) or str(tagged) != declared:
        return fail(
            f"tag {tag!r} means version {tagged} but pyproject.toml says {declared!r} - "
            "bump the version (and uv.lock) before tagging"
        )
    init = Path("src/otelfeature_instrument/__init__.py").read_text(encoding="utf-8")
    dunder = DUNDER.search(init)
    if not dunder or dunder.group(1) != declared:
        found = dunder.group(1) if dunder else None
        return fail(f"__version__ is {found!r} but pyproject.toml says {declared!r}")
    print(f"tag {tag} matches version {declared}", file=sys.stderr)
    print(f"prerelease={'true' if tagged.is_prerelease else 'false'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check_tag_version.py <tag>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
