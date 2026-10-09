#!/usr/bin/env python3
"""Bump the lockstep opentelemetry-* family together in pyproject.toml + uv.lock.

The OpenTelemetry Python packages release in lockstep and constrain each other
to the same minor version (e.g. opentelemetry-sdk 1.44.x requires
opentelemetry-api >=1.44.0,<1.45.dev0). Because they are pinned with ``==`` in
pyproject.toml, no single package can be upgraded on its own — and Dependabot's
uv updater resolves each dependency update individually
(https://github.com/dependabot/dependabot-core/issues/16390), so it cannot
update them either.

This script performs the upgrade the only way that works — the whole family at
once:

  1. Rewrite the opentelemetry-* ``==`` pins in pyproject.toml as permissive
     ranges (``>= current, < next major``).
  2. Run ``uv lock --upgrade-package <pkg>`` for every family member so the
     resolver picks the latest mutually-consistent version set.
  3. Rewrite the pins to the exact versions now locked.
  4. Re-lock to prove the exact pins are satisfiable.

Exits 0 whether or not anything changed; the caller (CI) decides what to do
with the resulting git diff. Run it from the repository root.
"""

import re
import subprocess
import sys
import tomllib

PYPROJECT = "pyproject.toml"
UVLOCK = "uv.lock"

# Matches e.g. "opentelemetry-sdk==1.44.0" (also pre-release "0.65b0").
PIN_RE = re.compile(r'"(opentelemetry-[a-z0-9-]+)==([0-9][0-9a-zA-Z.+!]*)"')


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def next_major(ver: str) -> str:
    """Upper bound for the relaxed range: 1.44.0 -> 2, 0.65b0 -> 1."""
    return str(int(re.split(r"[.ab!+]", ver)[0]) + 1)


def main() -> None:
    with open(PYPROJECT) as f:
        text = f.read()

    pins = dict(PIN_RE.findall(text))
    if not pins:
        sys.exit("no opentelemetry-* == pins found in pyproject.toml")

    # 1. Relax the pins so the resolver may move the whole family.
    relaxed = text
    for name, ver in pins.items():
        relaxed = relaxed.replace(
            f'"{name}=={ver}"', f'"{name}>={ver},<{next_major(ver)}"'
        )
    with open(PYPROJECT, "w") as f:
        f.write(relaxed)

    # 2. Upgrade the family together (transitive followers move as needed).
    run("uv", "lock", "--quiet", *(a for name in pins for a in ("--upgrade-package", name)))

    # 3. Read the locked versions.
    with open(UVLOCK, "rb") as f:
        locked = {p["name"]: p["version"] for p in tomllib.load(f)["package"]}

    # 4. Re-pin exactly at the locked versions.
    final = relaxed
    for name, ver in pins.items():
        final = final.replace(
            f'"{name}>={ver},<{next_major(ver)}"', f'"{name}=={locked[name]}"'
        )
    with open(PYPROJECT, "w") as f:
        f.write(final)

    # 5. Prove the exact pins resolve (uv.lock content stays as-is).
    run("uv", "lock", "--quiet")

    for name, old in sorted(pins.items()):
        new = locked[name]
        arrow = "->" if new != old else "=="
        print(f"{name}: {old} {arrow} {new}")


if __name__ == "__main__":
    main()
