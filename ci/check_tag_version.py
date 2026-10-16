"""Fail unless a release tag matches the package version exactly.

    python ci/check_tag_version.py v0.3.0

Accepted tags (spec C-006): `v<version>` for a release and `v<version>-rc.N`
for a pre-release of the same version, where `<version>` is `version` in
pyproject.toml, character for character - an rc rehearses the very release
it precedes (`v0.3.0-rc.1`, then `v0.3.0`). `__version__` in the package
must say the same. Prints `prerelease=true|false`, which the release
workflow appends to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

DUNDER = re.compile(r'^__version__ = "([^"]+)"$', re.M)


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def check(tag: str, root: Path = Path(".")) -> tuple[int, bool]:
    """(exit code, prerelease)."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject.get("project", {}).get("version", "")
    if not version:
        return fail("pyproject.toml has no project.version"), False
    # re.escape, not a hand-rolled substitution: every '.' must be a literal
    # dot, or v0x3x0 would match 0.3.0.
    match = re.fullmatch("v" + re.escape(version) + r"(-rc\.(?:0|[1-9][0-9]*))?", tag)
    if not match:
        return fail(
            f"tag {tag!r} is neither v{version} nor v{version}-rc.N - "
            "bump the version (and uv.lock) before tagging"
        ), False
    init = (root / "src/otelfeature_instrument/__init__.py").read_text(encoding="utf-8")
    dunder = DUNDER.search(init)
    if not dunder or dunder.group(1) != version:
        found = dunder.group(1) if dunder else None
        return fail(f"__version__ is {found!r} but pyproject.toml says {version!r}"), False
    prerelease = match.group(1) is not None
    print(f"tag {tag} matches version {version}", file=sys.stderr)
    return 0, prerelease


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_tag_version.py <tag>", file=sys.stderr)
        return 2
    code, prerelease = check(argv[1])
    if code == 0:
        print(f"prerelease={'true' if prerelease else 'false'}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
