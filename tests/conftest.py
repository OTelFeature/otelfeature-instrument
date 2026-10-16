"""Shared test helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write_flags(path: Path, flags: dict) -> None:
    """Rewrite a flagd flag file so flagd's file watcher is guaranteed to notice.

    The watcher reloads only when the file's mtime *increases*, and Linux file
    timestamps are coarse (one kernel tick): a rewrite right after the
    previous load can keep the same mtime and is then ignored for good. Use
    this for every rewrite of a file a provider has already loaded.
    """
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps(flags), encoding="utf-8")
    if path.stat().st_mtime_ns <= before:
        bumped = before + 10_000_000
        os.utime(path, ns=(bumped, bumped))


@pytest.fixture
def write_flags():
    return _write_flags
